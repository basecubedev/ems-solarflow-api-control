# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-secret discovery preparation metadata, persisted in the EMS config area.

This is the EMS-owned, redaction-safe half of discovery preparation: source
priority, per-source enable flags, local API scan ranges/manual hosts, and the
local MQTT broker list (id/label/host/port/tls + a ``credentials_ref`` pointing
at an entry in the credential store). It is stored beside the EMS config, in
``config/discovery-connections.json``, so a later EMS runtime can read it;
secrets never live here (only references do).

It is kept out of ``config/config.json`` on purpose: that file is the
safety-critical control config, and discovery preparation must never create or
rewrite it (see ``test_discovery_endpoints_write_no_ems_config``).

A brand-new install that only has an Admin-local ``discovery-preparation.json``
(priority + enable flags) is migrated in on first read, without deleting the
source.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ems.config import (
    default_mqtt_port,
    mqtt_tls_mode_name,
    normalize_mqtt_tls_mode,
    parse_mqtt_port,
    resolve_mqtt_tls_metadata,
)

from admin.discovery_preparation import (
    DEFAULT_PRIORITY,
    DISCOVERY_SOURCES,
    SOURCE_LOCAL_API,
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_MQTT,
)

CONNECTIONS_FILENAME = "discovery-connections.json"
DEFAULT_MQTT_PORT = 1883


def default_connections_path():
    # Reuse the EMS-owned config-dir resolver (honours EMS_INSTALL_DIR/
    # EMS_CONFIG_DIR) so this file lands in the same writable location as the
    # credential store. Falling back to ems.paths.BASE_DIR/config here would
    # target the read-only /app path inside the Admin container.
    from admin.credential_store import default_config_dir

    return default_config_dir() / CONNECTIONS_FILENAME


class DiscoveryConnectionsError(Exception):
    """A save failure whose message is safe to show to the operator."""


def _normalize_tls_mode(value, tls):
    """Stored mode name for a saved connection, never a downgrade.

    The mode vocabulary is Core's; an unresolvable value keeps verified TLS,
    because claiming less verification than the operator asked for is the one
    reading that could weaken a saved endpoint.
    """

    if not tls:
        return None
    try:
        _tls, insecure = normalize_mqtt_tls_mode(value)
    except ValueError:
        insecure = False
    return mqtt_tls_mode_name(tls=True, tls_insecure=insecure)


@dataclass(frozen=True)
class DiscoveryBrokerConfig:
    id: str
    label: str
    host: str
    port: int
    tls: bool
    tls_mode: str | None = None
    credentials_ref: str | None = None

    def to_dict(self):
        return {
            "id": self.id,
            "label": self.label,
            "host": self.host,
            "port": self.port,
            "tls": self.tls,
            "tls_mode": self.tls_mode,
            "credentials_ref": self.credentials_ref,
        }


@dataclass(frozen=True)
class DiscoveryPreparationConfig:
    priority: tuple[str, ...]
    local_api_enabled: bool
    local_mqtt_enabled: bool
    zendure_mqtt_enabled: bool
    local_mqtt_brokers: tuple[DiscoveryBrokerConfig, ...] = field(default_factory=tuple)


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    return default


def _normalize_priority(raw):
    priority = []
    seen = set()
    for entry in raw or []:
        if entry in DISCOVERY_SOURCES and entry not in seen:
            seen.add(entry)
            priority.append(entry)
    for source in DEFAULT_PRIORITY:
        if source not in seen:
            priority.append(source)
    return priority


def _slugify(text):
    cleaned = "".join(
        char if char.isalnum() else "-" for char in str(text or "").strip().lower()
    )
    return "-".join(part for part in cleaned.split("-") if part) or "broker"


def _normalize_broker(raw, used_ids):
    if not isinstance(raw, dict):
        return None
    host = str(raw.get("host") or "").strip()
    if not host:
        return None
    label = str(raw.get("label") or "").strip() or host
    broker_id = _slugify(raw.get("id") or label or host)
    base_id = broker_id
    suffix = 2
    while broker_id in used_ids:
        broker_id = f"{base_id}-{suffix}"
        suffix += 1
    used_ids.add(broker_id)
    try:
        tls, _tls_insecure = resolve_mqtt_tls_metadata(
            tls_mode=raw.get("tls_mode"), tls=raw.get("tls")
        )
        port = parse_mqtt_port(
            raw.get("port"), default=default_mqtt_port(tls)
        )
    except ValueError:
        return None
    credentials_ref = raw.get("credentials_ref")
    credentials_ref = str(credentials_ref).strip() or None if credentials_ref else None
    return DiscoveryBrokerConfig(
        id=broker_id,
        label=label,
        host=host,
        port=port,
        tls=tls,
        tls_mode=_normalize_tls_mode(raw.get("tls_mode"), tls),
        credentials_ref=credentials_ref,
    )


def _normalize_string_list(raw):
    if not isinstance(raw, list):
        return []
    items = []
    for entry in raw:
        text = str(entry or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def default_connections():
    return {
        "discovery_priority": list(DEFAULT_PRIORITY),
        "sources": {source: {"enabled": True} for source in DISCOVERY_SOURCES},
        "local_api": {"enabled": True, "scan_ranges": [], "manual_hosts": []},
        "local_mqtt": {"enabled": True, "credential_refs": [], "brokers": []},
        "zendure_mqtt": {"enabled": True, "token_ref": None},
    }


def normalize_connections(raw):
    """Coerce arbitrary input into a complete, valid connections payload.

    Accepts both the stored shape (``priority`` + per-source sections) and the
    legacy preparation shape (``discovery_priority`` + ``sources``); the
    ``sources`` map is always re-derived from the per-source ``enabled`` flags.
    """

    if not isinstance(raw, dict):
        raw = {}

    priority = _normalize_priority(raw.get("priority") or raw.get("discovery_priority"))

    legacy_sources = raw.get("sources")
    legacy_sources = legacy_sources if isinstance(legacy_sources, dict) else {}

    def _section_enabled(section_key, source_key):
        section = raw.get(section_key)
        if isinstance(section, dict) and "enabled" in section:
            return _as_bool(section["enabled"], True)
        entry = legacy_sources.get(source_key)
        if isinstance(entry, dict) and "enabled" in entry:
            return _as_bool(entry["enabled"], True)
        if isinstance(entry, bool):
            return entry
        return True

    local_api_raw = raw.get("local_api") if isinstance(raw.get("local_api"), dict) else {}
    local_mqtt_raw = (
        raw.get("local_mqtt") if isinstance(raw.get("local_mqtt"), dict) else {}
    )
    zendure_raw = (
        raw.get("zendure_mqtt") if isinstance(raw.get("zendure_mqtt"), dict) else {}
    )

    used_ids = set()
    brokers = []
    for broker_raw in local_mqtt_raw.get("brokers") or []:
        broker = _normalize_broker(broker_raw, used_ids)
        if broker is not None:
            brokers.append(broker)

    credential_refs = _normalize_string_list(local_mqtt_raw.get("credential_refs"))

    token_ref = zendure_raw.get("token_ref")
    token_ref = str(token_ref).strip() or None if token_ref else None

    local_api_enabled = _section_enabled("local_api", SOURCE_LOCAL_API)
    local_mqtt_enabled = _section_enabled("local_mqtt", SOURCE_LOCAL_MQTT)
    zendure_enabled = _section_enabled("zendure_mqtt", SOURCE_ZENDURE_MQTT)

    return {
        "discovery_priority": priority,
        "sources": {
            SOURCE_LOCAL_API: {"enabled": local_api_enabled},
            SOURCE_LOCAL_MQTT: {"enabled": local_mqtt_enabled},
            SOURCE_ZENDURE_MQTT: {"enabled": zendure_enabled},
        },
        "local_api": {
            "enabled": local_api_enabled,
            "scan_ranges": _normalize_string_list(local_api_raw.get("scan_ranges")),
            "manual_hosts": _normalize_string_list(local_api_raw.get("manual_hosts")),
        },
        "local_mqtt": {
            "enabled": local_mqtt_enabled,
            "credential_refs": credential_refs,
            # Legacy per-broker connection entries are tolerated on read for
            # backward compatibility, but are no longer part of the Discovery
            # flow (credentials are an endpoint-independent pool).
            "brokers": [broker.to_dict() for broker in brokers],
        },
        "zendure_mqtt": {"enabled": zendure_enabled, "token_ref": token_ref},
    }


def _persisted_shape(normalized):
    """The redundant ``sources`` mirror is dropped; ``priority`` is canonical."""

    return {
        "version": 1,
        "priority": list(normalized["discovery_priority"]),
        "local_api": dict(normalized["local_api"]),
        "local_mqtt": dict(normalized["local_mqtt"]),
        "zendure_mqtt": dict(normalized["zendure_mqtt"]),
    }


def preparation_config(normalized):
    """Return the structured :class:`DiscoveryPreparationConfig` view."""

    brokers = tuple(
        DiscoveryBrokerConfig(
            id=item["id"],
            label=item["label"],
            host=item["host"],
            port=item["port"],
            tls=item["tls"],
            tls_mode=item.get("tls_mode"),
            credentials_ref=item.get("credentials_ref"),
        )
        for item in normalized["local_mqtt"]["brokers"]
    )
    return DiscoveryPreparationConfig(
        priority=tuple(normalized["discovery_priority"]),
        local_api_enabled=normalized["local_api"]["enabled"],
        local_mqtt_enabled=normalized["local_mqtt"]["enabled"],
        zendure_mqtt_enabled=normalized["zendure_mqtt"]["enabled"],
        local_mqtt_brokers=brokers,
    )


class DiscoveryConnectionsStore:
    """Persists discovery connection metadata in ``config/config.json``.

    Reads degrade to defaults on a missing/corrupt config; writes preserve every
    other config key and only replace the ``discovery_connections`` block.
    """

    def __init__(self, path=None, *, legacy_preparation_store=None):
        self.path = Path(path) if path else default_connections_path()
        self._legacy_preparation_store = legacy_preparation_store

    def load(self):
        block = self._read_block()
        if block is None:
            block = self._legacy_block()
        return normalize_connections(block or {})

    def save(self, payload):
        merged = self._merge(self.load(), payload)
        normalized = normalize_connections(merged)
        self._write_block(normalized)
        return normalized

    # --- broker / token helpers ------------------------------------------

    def upsert_broker(self, broker):
        current = self.load()
        brokers = [
            item
            for item in current["local_mqtt"]["brokers"]
            if item["id"] != broker.get("id")
        ]
        brokers.append(dict(broker))
        current["local_mqtt"]["brokers"] = brokers
        normalized = normalize_connections(current)
        self._write_block(normalized)
        return normalized

    def remove_broker(self, broker_id):
        current = self.load()
        removed = None
        kept = []
        for item in current["local_mqtt"]["brokers"]:
            if item["id"] == broker_id:
                removed = item
            else:
                kept.append(item)
        current["local_mqtt"]["brokers"] = kept
        self._write_block(normalize_connections(current))
        return removed

    def add_credential_ref(self, ref):
        ref = str(ref or "").strip()
        if not ref:
            return self.load()
        current = self.load()
        refs = list(current["local_mqtt"]["credential_refs"])
        if ref not in refs:
            refs.append(ref)
        current["local_mqtt"]["credential_refs"] = refs
        normalized = normalize_connections(current)
        self._write_block(normalized)
        return normalized

    def remove_credential_ref(self, ref):
        ref = str(ref or "").strip()
        current = self.load()
        refs = [r for r in current["local_mqtt"]["credential_refs"] if r != ref]
        removed = len(refs) != len(current["local_mqtt"]["credential_refs"])
        current["local_mqtt"]["credential_refs"] = refs
        self._write_block(normalize_connections(current))
        return removed

    def set_zendure_token_ref(self, token_ref):
        current = self.load()
        current["zendure_mqtt"]["token_ref"] = token_ref
        normalized = normalize_connections(current)
        self._write_block(normalized)
        return normalized

    # --- internals -------------------------------------------------------

    def _merge(self, current, payload):
        if not isinstance(payload, dict):
            return current
        merged = normalize_connections(current)
        source = normalize_connections(payload)
        # Priority and enable flags always come from the incoming payload; the
        # broker list is only replaced when the payload actually carries one.
        merged["discovery_priority"] = source["discovery_priority"]
        merged["local_api"]["enabled"] = source["local_api"]["enabled"]
        merged["local_mqtt"]["enabled"] = source["local_mqtt"]["enabled"]
        merged["zendure_mqtt"]["enabled"] = source["zendure_mqtt"]["enabled"]
        if isinstance(payload.get("local_api"), dict):
            for key in ("scan_ranges", "manual_hosts"):
                if key in payload["local_api"]:
                    merged["local_api"][key] = source["local_api"][key]
        if isinstance(payload.get("local_mqtt"), dict):
            if "brokers" in payload["local_mqtt"]:
                merged["local_mqtt"]["brokers"] = source["local_mqtt"]["brokers"]
            if "credential_refs" in payload["local_mqtt"]:
                merged["local_mqtt"]["credential_refs"] = source["local_mqtt"][
                    "credential_refs"
                ]
        if isinstance(payload.get("zendure_mqtt"), dict) and "token_ref" in payload["zendure_mqtt"]:
            merged["zendure_mqtt"]["token_ref"] = source["zendure_mqtt"]["token_ref"]
        return merged

    def _read_block(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _legacy_block(self):
        if self._legacy_preparation_store is None:
            return None
        try:
            prep = self._legacy_preparation_store.load()
        except Exception:
            return None
        if not isinstance(prep, dict):
            return None
        return {
            "priority": prep.get("discovery_priority"),
            "sources": prep.get("sources"),
        }

    def _write_block(self, normalized):
        data = _persisted_shape(normalized)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".discovery-connections.", suffix=".tmp", dir=self.path.parent
            )
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise DiscoveryConnectionsError(
                "Could not save the discovery connection settings."
            ) from exc
        return normalized
