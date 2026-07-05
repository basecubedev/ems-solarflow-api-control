# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lightweight MQTT broker candidate discovery."""

import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from admin.discovery import (
    TIMEOUT_MS_MIN,
    clamp_max_workers,
    clamp_timeout_ms,
    iter_scan_hosts,
    validate_cidr,
)
from admin.models import MqttBrokerCandidate, utc_now_iso

MQTT_MDNS_SERVICE_TYPE = "_mqtt._tcp.local."
MQTT_PORTS = (1883, 8883)
MQTT_TIMEOUT_MS_DEFAULT = 400
MQTT_TIMEOUT_MS_MAX = 1500
MQTT_MAX_WORKERS_DEFAULT = 32


def decode_mqtt_txt(properties):
    decoded = {}
    if not isinstance(properties, dict):
        return decoded
    for key, value in properties.items():
        text_key = key.decode("utf-8", "replace") if isinstance(key, bytes) else str(key)
        if isinstance(value, bytes):
            decoded[text_key] = value.decode("utf-8", "replace")
        elif value is None:
            decoded[text_key] = ""
        else:
            decoded[text_key] = str(value)
    return decoded


def build_mqtt_mdns_candidate(service_name, hostname, addresses, port, properties):
    """Normalize a resolved ``_mqtt._tcp`` service."""

    if not addresses or not port:
        return None
    return MqttBrokerCandidate(
        host=str(addresses[0]),
        port=int(port),
        hostname=str(hostname) if hostname else None,
        service_name=str(service_name) if service_name else None,
        source="mdns",
        status="reachable",
        confidence=0.75,
        details={"txt": decode_mqtt_txt(properties)},
    ).to_dict()


def tcp_port_open(host, port, timeout_s):
    try:
        connection = socket.create_connection((host, port), timeout=timeout_s)
    except OSError:
        return False
    try:
        return True
    finally:
        connection.close()


class MqttBrokerStore:
    """Thread-safe broker store deduplicated by host and port."""

    def __init__(self):
        self._lock = threading.Lock()
        self._candidates = {}

    def merge(self, candidate):
        key = candidate.get("id") or f"mqtt:{candidate['host']}:{candidate['port']}"
        incoming = dict(candidate)
        incoming["id"] = key
        with self._lock:
            existing = self._candidates.get(key)
            if existing:
                sources = list(existing.get("sources") or [existing.get("source")])
                for source in incoming.get("sources") or [incoming.get("source")]:
                    if source and source not in sources:
                        sources.append(source)
                merged = dict(existing)
                for field, value in incoming.items():
                    if value is not None:
                        merged[field] = value
                if (
                    existing.get("source") == "mdns"
                    and incoming.get("source") != "mdns"
                ):
                    merged["source"] = "mdns"
                    merged["hostname"] = existing.get("hostname")
                    merged["service_name"] = existing.get("service_name")
                    merged["details"] = existing.get("details", {})
                    merged["confidence"] = max(
                        existing.get("confidence", 0), incoming.get("confidence", 0)
                    )
                merged["sources"] = sources
                self._candidates[key] = merged
            else:
                incoming.setdefault("sources", [incoming.get("source")])
                self._candidates[key] = incoming
        return key

    def to_list(self):
        with self._lock:
            candidates = [dict(item) for item in self._candidates.values()]
        return sorted(candidates, key=lambda item: (item["host"], item["port"]))


class MqttBrokerDiscovery:
    def __init__(self, store=None, connector=tcp_port_open):
        self.store = store or MqttBrokerStore()
        self._connector = connector

    def add_mdns_candidate(self, candidate):
        if candidate:
            self.store.merge(candidate)

    def candidates(self):
        return self.store.to_list()

    def probe(self, cidr, timeout_ms=None, max_workers=None):
        network = validate_cidr(cidr)
        timeout_ms = clamp_timeout_ms(
            timeout_ms if timeout_ms is not None else MQTT_TIMEOUT_MS_DEFAULT,
            MQTT_TIMEOUT_MS_MAX,
        )
        timeout_ms = max(TIMEOUT_MS_MIN, timeout_ms)
        workers = clamp_max_workers(
            max_workers if max_workers is not None else MQTT_MAX_WORKERS_DEFAULT
        )
        endpoints = [
            (host, port) for host in iter_scan_hosts(network) for port in MQTT_PORTS
        ]
        found = []
        with ThreadPoolExecutor(max_workers=min(workers, len(endpoints))) as pool:
            futures = {
                pool.submit(self._connector, host, port, timeout_ms / 1000.0): (host, port)
                for host, port in endpoints
            }
            for future in as_completed(futures):
                host, port = futures[future]
                try:
                    reachable = future.result()
                except Exception:
                    reachable = False
                if not reachable:
                    continue
                candidate = MqttBrokerCandidate(
                    host=host,
                    port=port,
                    source="network_probe",
                    status="tcp_open",
                    confidence=0.6,
                ).to_dict()
                self.store.merge(candidate)
                found.append(candidate)
        return {
            "cidr": str(network),
            "ports": list(MQTT_PORTS),
            "probed": len(endpoints),
            "found": len(found),
            "candidates": self.candidates(),
        }

    def refresh(self):
        known = self.candidates()
        if not known:
            return {"checked": 0, "reachable": 0, "candidates": []}
        refreshed = 0
        timeout_s = MQTT_TIMEOUT_MS_DEFAULT / 1000.0
        with ThreadPoolExecutor(
            max_workers=min(MQTT_MAX_WORKERS_DEFAULT, len(known))
        ) as pool:
            futures = {
                pool.submit(self._connector, item["host"], item["port"], timeout_s): item
                for item in known
            }
            for future in as_completed(futures):
                item = dict(futures[future])
                try:
                    item["reachable"] = bool(future.result())
                except Exception:
                    item["reachable"] = False
                item["status"] = "reachable" if item["reachable"] else "unreachable"
                if item["reachable"]:
                    item["last_seen"] = utc_now_iso()
                    refreshed += 1
                self.store.merge(item)
        return {
            "checked": len(known),
            "reachable": refreshed,
            "candidates": self.candidates(),
        }
