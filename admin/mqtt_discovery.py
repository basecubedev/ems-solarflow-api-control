# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lightweight MQTT broker candidate discovery."""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ems.config import default_mqtt_port, parse_mqtt_port, resolve_mqtt_tls_metadata

from admin.discovery import (
    TIMEOUT_MS_MIN,
    clamp_max_workers,
    clamp_timeout_ms,
    iter_scan_hosts,
    validate_cidr,
)
from admin.models import MqttBrokerCandidate, utc_now_iso

MQTT_MDNS_SERVICE_TYPE = "_mqtt._tcp.local."
MQTT_TLS_PORT = 8883
MQTT_PORTS = (1883, MQTT_TLS_PORT)
MQTT_TIMEOUT_MS_DEFAULT = 400
MQTT_TIMEOUT_MS_MAX = 1500
MQTT_MAX_WORKERS_DEFAULT = 32

# Per-attempt statuses that mean the topic listener actually connected and
# observed the broker (even if it saw no topics). Any other status means topic
# discovery did not succeed for that attempt, so its device view is not trusted.
_LISTENED_STATUSES = frozenset({"topics_seen", "mqtt_listened_no_topics"})


def transport_for_port(port):
    """The default MQTT transport implied by a port (8883 is TLS)."""

    return "tls" if int(port) == MQTT_TLS_PORT else "plaintext"


def _transport_for_broker(broker):
    """Infer the transport for an endpoint (explicit ``tls`` overrides the port)."""

    tls = broker.get("tls")
    if tls is not None:
        return "tls" if tls else "plaintext"
    return transport_for_port(broker.get("port") or 0)


def _device_key(device):
    return (
        device.get("id")
        or device.get("serial_number")
        or device.get("device_id")
        or id(device)
    )


def _normalize_attempt_result(raw):
    """Coerce a topic-discoverer return into ``{status, devices}``.

    Accepts either a list of device dicts (legacy) or a structured
    ``{status, devices}`` dict, so richer per-attempt statuses can be surfaced
    without breaking the simple list contract.
    """

    if isinstance(raw, dict):
        devices = raw.get("devices")
        devices = devices if isinstance(devices, list) else []
        status = raw.get("status") or (
            "topics_seen" if devices else "mqtt_listened_no_topics"
        )
        return {"status": status, "devices": devices}
    devices = raw if isinstance(raw, list) else []
    return {
        "status": "topics_seen" if devices else "mqtt_listened_no_topics",
        "devices": devices,
    }


def _attempt_record(broker, transport, attempt, result):
    """Redacted, stable per-attempt metadata (never a username or password)."""

    ref = attempt["credential_ref"]
    who = "anonymous" if ref is None else f"credential:{ref}"
    identity = f"mqtt-probe:{broker.get('host')}:{broker.get('port')}:{transport}:{who}"
    return {
        "attempt_id": identity,
        "credential_ref": ref,
        "label": attempt["label"],
        "transport": transport,
        "status": result["status"],
        "device_count": len(result["devices"]),
    }


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

    def __init__(self, *, clock=None, proposal_ttl_seconds=900):
        self._lock = threading.Lock()
        self._candidates = {}
        self._clock = clock or time.time
        self._proposal_ttl_seconds = float(proposal_ttl_seconds)
        self._generation = 0
        self._refresh_in_progress = False
        self._refresh_started_at = None
        self._refresh_completed_at = None
        self._topic_refresh_success = None

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
            generation = self._generation
            in_progress = self._refresh_in_progress
            completed_at = self._refresh_completed_at
            success = self._topic_refresh_success
            now = self._clock()
        # Generation-wide gates that make no candidate selectable regardless of
        # its per-broker result: a refresh in progress, none ever completed, or
        # the proposal window has expired.
        expires_at = (
            completed_at + self._proposal_ttl_seconds if completed_at is not None else None
        )
        window_open = (
            not in_progress
            and completed_at is not None
            and expires_at is not None
            and now < expires_at
        )
        for candidate in candidates:
            cand_success = self._candidate_topic_success(candidate, success)
            candidate["discovery_generation"] = generation
            candidate["refresh_started_at"] = self._refresh_started_at
            candidate["refresh_completed_at"] = completed_at
            candidate["topic_refresh_success"] = cand_success
            candidate["proposal_created_at"] = completed_at
            candidate["proposal_expires_at"] = expires_at
            # A broker's own devices are only selectable when the window is open
            # AND that broker was reachable and refreshed its topics in the
            # current generation. A different broker succeeding never validates it.
            if not (window_open and cand_success):
                candidate["devices"] = []
        return sorted(candidates, key=lambda item: (item["host"], item["port"]))

    @staticmethod
    def _candidate_topic_success(candidate, global_success):
        """Per-broker current-generation topic-refresh validity.

        Uses the candidate's own ``reachable``/``topic_refresh_success`` result
        when present (the live refresh path always sets these). Legacy callers of
        :meth:`complete_refresh` that stage items without a per-broker result fall
        back to the generation-wide ``success`` flag.
        """

        reachable = candidate.get("reachable")
        topic_success = candidate.get("topic_refresh_success")
        if reachable is None and topic_success is None:
            return bool(global_success)
        return bool(reachable) and bool(topic_success)

    def begin_refresh(self):
        with self._lock:
            self._generation += 1
            self._refresh_in_progress = True
            self._refresh_started_at = self._clock()
            self._refresh_completed_at = None
            self._topic_refresh_success = None
            return self._generation

    def complete_refresh(self, generation, candidates, *, success):
        with self._lock:
            if generation != self._generation:
                return False
            self._candidates = {
                item.get("id") or f"mqtt:{item['host']}:{item['port']}": dict(item)
                for item in candidates
            }
            self._refresh_in_progress = False
            self._refresh_completed_at = self._clock()
            self._topic_refresh_success = bool(success)
            return True


class MqttBrokerDiscovery:
    def __init__(
        self,
        store=None,
        connector=tcp_port_open,
        topic_discoverer=None,
        *,
        credential_lookup=None,
        credential_refs_provider=None,
    ):
        self.store = store or MqttBrokerStore()
        self._connector = connector
        # Optional read-only per-broker hardware/topic discovery. Left unset in
        # unit construction; the Admin runtime wires the paho-backed default.
        self._topic_discoverer = topic_discoverer
        # Optional ``credential_ref -> MqttBrokerSecret`` resolver for the shared
        # discovery credential pool. The resolved secret is only ever handed to a
        # transient per-attempt broker copy, never merged into a stored candidate.
        self._credential_lookup = credential_lookup
        # Optional callable returning the current pool of discovery credential
        # refs. Read fresh on every refresh so the matrix always reflects the
        # latest saved credentials.
        self._credential_refs_provider = credential_refs_provider

    def add_mdns_candidate(self, candidate):
        if candidate:
            self.store.merge(candidate)

    def set_configured_brokers(self, brokers):
        """Merge persisted local MQTT brokers (credential refs, no secrets)."""

        for broker in brokers or []:
            host = broker.get("host")
            if not host:
                continue
            try:
                tls, tls_insecure = resolve_mqtt_tls_metadata(
                    tls_mode=broker.get("tls_mode"),
                    tls=broker.get("tls"),
                    tls_insecure=broker.get("tls_insecure"),
                )
                port = parse_mqtt_port(
                    broker.get("port"), default=default_mqtt_port(tls)
                )
            except ValueError:
                continue
            self.store.merge(
                {
                    "id": broker.get("id") or f"mqtt:{host}:{broker.get('port')}",
                    "host": host,
                    "port": port,
                    "label": broker.get("label"),
                    "tls": tls,
                    "tls_insecure": tls_insecure,
                    "tls_mode": broker.get("tls_mode") if tls else None,
                    "transport": "tls" if tls else "plaintext",
                    "auth_mode": (
                        "username_password"
                        if broker.get("credentials_ref")
                        else "anonymous"
                    ),
                    "credentials_ref": broker.get("credentials_ref"),
                    "source": "configured",
                    "status": "configured",
                    "confidence": 0.5,
                }
            )

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
        open_by_port = {port: 0 for port in MQTT_PORTS}
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
                open_by_port[port] += 1
                candidate = MqttBrokerCandidate(
                    host=host,
                    port=port,
                    source="network_probe",
                    status="tcp_open",
                    confidence=0.6,
                    transport=transport_for_port(port),
                    auth_mode="anonymous",
                    mqtt_connect_status="tcp_open_only",
                ).to_dict()
                self.store.merge(candidate)
                found.append(candidate)
        checked_per_port = len(endpoints) // len(MQTT_PORTS) if MQTT_PORTS else 0
        return {
            "cidr": str(network),
            "ports": list(MQTT_PORTS),
            "probed": len(endpoints),
            "found": len(found),
            "tested_combinations": [
                {
                    "port": port,
                    "transport": transport_for_port(port),
                    "auth_mode": "anonymous",
                    "checked_hosts": checked_per_port,
                    "open_endpoints": open_by_port[port],
                }
                for port in MQTT_PORTS
            ],
            "candidates": self.candidates(),
        }

    def refresh(self):
        known = self.candidates()
        if not known:
            return {"checked": 0, "reachable": 0, "devices_found": 0, "candidates": []}
        generation = self.store.begin_refresh()
        staged = []
        refreshed = 0
        devices_found = 0
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
                item["candidate_generation"] = generation
                prior_devices = item.get("devices") or []
                try:
                    item["reachable"] = bool(future.result())
                except Exception:
                    item["reachable"] = False
                item["status"] = "reachable" if item["reachable"] else "unreachable"
                if item["reachable"]:
                    item["last_seen"] = utc_now_iso()
                    refreshed += 1
                    devices_found += self._discover_topics(item)
                    if not item.get("topic_refresh_success"):
                        # Reachable at TCP but topics never listened: keep the
                        # prior devices only as history, never as selectable.
                        item["last_known_devices"] = prior_devices
                else:
                    # A broker that dropped out of this generation must not keep
                    # offering its previous generation's devices as proposals.
                    item["topic_refresh_success"] = False
                    item["last_known_devices"] = prior_devices
                    item["devices"] = []
                staged.append(item)
        self.store.complete_refresh(generation, staged, success=refreshed > 0)
        return {
            "checked": len(known),
            "reachable": refreshed,
            "devices_found": devices_found,
            "candidates": self.candidates(),
        }

    def _discover_topics(self, broker):
        """Probe a reachable broker with the anonymous + credential-pool matrix.

        For the endpoint's inferred transport we always try anonymous first, then
        every saved discovery credential; failures are contained per attempt and
        per broker so one attempt/broker can never break refresh. Deduplicated
        hardware candidates and redacted attempt records are attached in place.
        """

        transport = _transport_for_broker(broker)
        broker["transport"] = transport
        attempts = []
        devices_by_id = {}
        for attempt in self._credential_attempts():
            attempt_broker = {
                **broker,
                "tls": transport == "tls",
                "username": attempt["username"],
                "password": attempt["password"],
                # Non-secret pool id of the credential this attempt uses, so a
                # candidate discovered with it can preserve credentials_ref.
                "credentials_ref": attempt["credential_ref"],
            }
            result = self._run_attempt(attempt_broker)
            for device in result["devices"]:
                devices_by_id.setdefault(_device_key(device), device)
            attempts.append(_attempt_record(broker, transport, attempt, result))
        broker["attempts"] = attempts
        # Topic discovery succeeded only if at least one attempt actually listened
        # to the broker; if every attempt failed to connect, this broker's device
        # view is untrusted for the current generation.
        broker["topic_refresh_success"] = any(
            record["status"] in _LISTENED_STATUSES for record in attempts
        )
        broker["devices"] = list(devices_by_id.values())
        # No secret ever survives the attempt: strip any transient auth fields.
        broker.pop("username", None)
        broker.pop("password", None)
        return len(broker["devices"])

    def _run_attempt(self, attempt_broker):
        if self._topic_discoverer is None:
            return {"status": "tcp_open", "devices": []}
        try:
            raw = self._topic_discoverer(attempt_broker)
        except Exception:
            return {"status": "connection_failed", "devices": []}
        return _normalize_attempt_result(raw)

    def _credential_attempts(self):
        """Yield anonymous first, then one attempt per saved discovery credential.

        Anonymous is always attempted, and every credential is attempted
        independently, so a failed credential never suppresses the next one.
        """

        yield {
            "credential_ref": None,
            "label": "anonymous",
            "username": None,
            "password": None,
        }
        seen = set()
        for ref in self._current_credential_refs():
            if not ref or ref in seen:
                continue
            seen.add(ref)
            secret = self._lookup_secret(ref)
            if secret is None:
                continue
            yield {
                "credential_ref": ref,
                "label": getattr(secret, "label", None) or ref,
                "username": getattr(secret, "username", None),
                "password": getattr(secret, "password", None),
            }

    def _current_credential_refs(self):
        if self._credential_refs_provider is None:
            return []
        try:
            refs = self._credential_refs_provider()
        except Exception:
            return []
        return [str(ref) for ref in refs or []]

    def _lookup_secret(self, ref):
        if self._credential_lookup is None:
            return None
        try:
            return self._credential_lookup(ref)
        except Exception:
            return None
