# SPDX-License-Identifier: AGPL-3.0-or-later
"""The brokers an MQTT mapping can read, and how a new one is found and kept.

The mapping shows one list of four kinds:

* ``builtin``: the Mosquitto the Admin runs;
* ``profile``: a Local MQTT profile the EMS already has;
* ``saved``: a broker a mapping already reads, remembered in ``mappings.json``
  under ``brokers`` (its endpoint and a ``credentials_ref``, never a secret);
* ``found``: a broker a network search, Find devices or the operator named,
  not read by any mapping yet.

A found broker is checked with a real MQTT CONNECT: anonymously first, then
with each account of the discovery pool. Discovery's own listening cannot tell
a refused account from a quiet broker, so its result is not trusted here.
Saving the first mapping on a found broker remembers it, and its account stays
in the discovery pool. Adding the published device through Find devices then
gives the EMS a profile of its own the usual way.
"""

import contextlib
import json
import re
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from admin.mqtt_broker import BROKER_SERVICE, DEFAULT_PORT, local_profile_endpoints, this_broker_endpoints
from admin.zendure_mqtt_broker_profiles import valid_host
from ems.file_io import atomic_write_bytes
from ems.zendure_mqtt.builtin_broker import BUILTIN_PROFILE_REF, builtin_profile_refs, mappings_path
from ems.zendure_mqtt.config_entries import (
    config_entry_enabled,
    is_external_mqtt_device_config,
    zendure_mqtt_broker_ref,
    zendure_mqtt_route_device_id,
    zendure_mqtt_topic_family,
)
from ems.zendure_mqtt.external_catalog import EXTERNAL_TOPIC_FAMILIES
from mqtt_mapping.config import read_mappings_document, saved_brokers

ACCESS_STATES = ("anonymous", "account", "needs_account", "untrusted", "unreachable", "unchecked")
USABLE_ACCESS = ("anonymous", "account")
ERROR_CODES = (
    "mqtt_mapping_broker_unknown",
    "mqtt_mapping_broker_refused",
    "mqtt_mapping_broker_unreachable",
    "mqtt_mapping_broker_endpoint_invalid",
    "mqtt_mapping_broker_account_invalid",
    "mqtt_mapping_broker_search_busy",
)
FOUND_REF_PREFIX = "broker-"
LOGIN_TIMEOUT_SECONDS = 3.0
SEARCH_WORKERS = 8
MAX_ACCOUNT_LENGTH = 256
BUILTIN_IDENTITY = "builtin"


def found_ref(host, port) -> str:
    """The name a found broker keeps in the mapping and in the discovery pool."""

    slug = re.sub(r"[^a-z0-9]+", "-", str(host).lower()).strip("-")[:48] or "host"
    return f"{FOUND_REF_PREFIX}{slug}-{int(port)}"


def paho_login(endpoint, timeout=LOGIN_TIMEOUT_SECONDS) -> str:
    """One CONNECT with the endpoint's account: ``accepted``, ``refused``, ``untrusted`` or ``unreachable``.

    ``untrusted`` is a TLS broker whose certificate does not verify: it
    answers, but only an operator who accepts the certificate may read it.
    """

    import paho.mqtt.client as mqtt

    from ems.config import configure_mqtt_client_tls

    answered = threading.Event()
    outcome = {"state": "unreachable"}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    configure_mqtt_client_tls(client, tls=bool(endpoint.get("tls")), tls_insecure=bool(endpoint.get("tls_insecure")))
    if endpoint.get("username"):
        client.username_pw_set(endpoint["username"], endpoint.get("password"))

    def on_connect(_client, _userdata, _flags, reason, _properties=None):
        outcome["state"] = "refused" if getattr(reason, "is_failure", False) else "accepted"
        answered.set()

    client.on_connect = on_connect
    try:
        client.connect(endpoint["host"], int(endpoint["port"]), keepalive=10)
    except ssl.SSLCertVerificationError:
        return "untrusted"
    except (OSError, ValueError):
        return "unreachable"
    client.loop_start()
    try:
        answered.wait(timeout)
    finally:
        client.loop_stop()
        with contextlib.suppress(Exception):
            client.disconnect()
    return outcome["state"]


@dataclass(frozen=True)
class FoundBroker:
    ref: str
    host: str
    port: int
    tls: bool = False
    tls_insecure: bool = False
    access: str = "unchecked"
    credentials_ref: str | None = None
    origin: str = "search"

    @property
    def usable(self) -> bool:
        return self.access in USABLE_ACCESS

    def entry(self) -> dict:
        """What ``mappings.json`` remembers once a mapping reads it."""

        return {
            "host": self.host,
            "port": self.port,
            "tls": self.tls,
            "tls_insecure": self.tls_insecure,
            "credentials_ref": self.credentials_ref,
        }


def _account_problem(username, password) -> bool:
    for value in (username, password):
        if not isinstance(value, str) or len(value) > MAX_ACCOUNT_LENGTH or any(ord(char) < 32 for char in value):
            return True
    return not username or not password


def _key(host, port):
    return str(host).strip().lower(), int(port)


class MappingBrokers:
    """The found brokers of this Admin process, and the checks that tell how a mapping may read each."""

    def __init__(self, *, discovery, credential_store, preparation, login=paho_login, networks=None, workers=SEARCH_WORKERS):
        from admin.networks import detect_network_suggestions

        self._discovery = discovery
        self._store = credential_store
        self._preparation = preparation
        self._login = login
        self._networks = networks or detect_network_suggestions
        self._workers = workers
        self._found = {}
        self._saved_access = {}
        self._lock = threading.Lock()
        self._searching = threading.Lock()

    # --- the list ---------------------------------------------------------

    def choices(self, broker, *, address=None) -> dict:
        """Built-in, profiles, saved and found, in the order the mapping shows them."""

        config = broker._config_loader()
        settings, _issues, _exists = broker.load()
        port = settings.port if settings is not None else DEFAULT_PORT
        running = broker.container_states().get(BROKER_SERVICE) == "running"
        enabled = bool(settings is not None and settings.enabled)
        items = [
            {
                "ref": BUILTIN_PROFILE_REF,
                "kind": "builtin",
                "host": address,
                "port": port,
                "state": "running" if running and enabled else "off" if not enabled else "stopped",
                "usable": running and enabled,
            }
        ]
        taken = {_key(*endpoint) for endpoint in this_broker_endpoints(port, self._local_addresses(address))}
        saved = saved_brokers(read_mappings_document(mappings_path(broker.config_dir)))
        for ref, entry in saved.items():
            item = self._known(ref, "saved", entry, account=self._saved_account(entry))
            with self._lock:
                checked = self._saved_access.get(ref)
            if checked is not None and checked[1] == self._saved_signature(entry):
                item = {**item, "access": checked[0], "usable": checked[0] in USABLE_ACCESS}
            items.append(item)
            taken.add(_key(entry["host"], entry["port"]))
        for ref, endpoint in local_profile_endpoints(config, self._store).items():
            if _key(endpoint["host"], endpoint["port"]) in taken:
                continue
            items.append(self._known(ref, "profile", endpoint, account=True))
            taken.add(_key(endpoint["host"], endpoint["port"]))
        with self._lock:
            self._adopt_discovered(taken)
            found = sorted(
                (item for item in self._found.values() if _key(item.host, item.port) not in taken),
                key=lambda item: (item.origin != "manual", item.host, item.port),
            )
        items.extend(self._found_view(item) for item in found)
        return {"brokers": items, "searching": self._searching.locked()}

    @staticmethod
    def _known(ref, kind, endpoint, *, account):
        return {
            "ref": ref,
            "kind": kind,
            "host": endpoint["host"],
            "port": endpoint["port"],
            "tls": bool(endpoint.get("tls")),
            "access": "account" if account else "needs_account",
            "usable": account,
        }

    def _local_addresses(self, address):
        """Every address of this host, so the built-in broker is never offered as a found one."""

        addresses = [address] if address else []
        with contextlib.suppress(Exception):
            addresses += [item["address"] for item in self._networks().get("networks") or [] if item.get("address")]
        return addresses

    def _saved_signature(self, entry):
        """What a check of a remembered broker held true: its account and whether that account is still stored."""

        return entry["credentials_ref"], self._saved_account(entry)

    def _note_saved(self, broker, ref, access):
        entry = saved_brokers(read_mappings_document(mappings_path(broker.config_dir))).get(ref)
        if entry is not None:
            with self._lock:
                self._saved_access[ref] = (access, self._saved_signature(entry))

    def _saved_account(self, entry) -> bool:
        if not entry["credentials_ref"]:
            return True
        secret = self._store.load_mqtt_discovery_secret(entry["credentials_ref"])
        return secret is not None and bool(secret.username)

    @staticmethod
    def _found_view(item):
        return {
            "ref": item.ref,
            "kind": "found",
            "host": item.host,
            "port": item.port,
            "tls": item.tls,
            "origin": item.origin,
            "access": item.access,
            "usable": item.usable,
        }

    def _adopt_discovered(self, taken):
        for candidate in self._discovery.candidates():
            host, port = candidate.get("host"), candidate.get("port")
            if not host or not isinstance(port, int) or _key(host, port) in taken:
                continue
            ref = found_ref(host, port)
            tls = candidate["tls"] if isinstance(candidate.get("tls"), bool) else candidate.get("transport") == "tls"
            if ref not in self._found:
                self._found[ref] = FoundBroker(
                    ref,
                    host,
                    port,
                    tls=tls,
                    tls_insecure=tls and bool(candidate.get("tls_insecure")),
                    origin="discovery",
                )

    def usable(self) -> dict:
        """``ref -> entry`` for every found broker a mapping may read now."""

        with self._lock:
            return {ref: item.entry() for ref, item in self._found.items() if item.usable}

    def endpoint(self, ref) -> dict | None:
        """The endpoint the live topics read a usable found broker with, its account included."""

        with self._lock:
            item = self._found.get(ref)
        if item is None or not item.usable:
            return None
        endpoint = {"host": item.host, "port": item.port, "tls": item.tls, "tls_insecure": item.tls_insecure}
        if item.credentials_ref:
            secret = self._store.load_mqtt_discovery_secret(item.credentials_ref)
            if secret is None or not secret.username:
                return None
            endpoint["username"], endpoint["password"] = secret.username, secret.password
        return endpoint

    # --- one device id, two brokers ---------------------------------------

    def _identities(self, broker, address):
        """``(host, port) -> identity``: ``builtin`` for every endpoint that reaches the built-in broker."""

        settings, _issues, _exists = broker.load()
        port = settings.port if settings is not None else DEFAULT_PORT
        local = {_key(*endpoint) for endpoint in this_broker_endpoints(port, self._local_addresses(address))}

        def identity(host, port_):
            if not host or not isinstance(port_, int):
                return None
            key = _key(host, port_)
            return BUILTIN_IDENTITY if key in local else f"{key[0]}:{key[1]}"

        return identity

    def ref_identity(self, broker, ref, *, address=None):
        """The broker a mapping's ``broker_ref`` reaches: ``builtin`` or ``host:port``; ``None`` when unknown."""

        if ref == BUILTIN_PROFILE_REF:
            return BUILTIN_IDENTITY
        identity = self._identities(broker, address)
        saved = saved_brokers(read_mappings_document(mappings_path(broker.config_dir))).get(ref)
        if saved is not None:
            return identity(saved["host"], saved["port"])
        profile = local_profile_endpoints(broker._config_loader(), self._store).get(ref)
        if profile is not None:
            return identity(profile["host"], profile["port"])
        with self._lock:
            found = self._found.get(ref)
        return identity(found.host, found.port) if found is not None else None

    def external_sources(self, broker, *, address=None) -> dict[str, dict]:
        """``device id -> {broker identity -> {"configured", "published"}}`` for every catalog device the EMS has or discovery saw.

        Ids are one namespace across the catalog families, as they are in the
        mapping file, so an inverter and a consumer never share one.
        """

        identity = self._identities(broker, address)
        config = broker._config_loader()
        section = config.get("zendure_mqtt") if isinstance(config.get("zendure_mqtt"), dict) else {}
        profiles = section.get("brokers") if isinstance(section.get("brokers"), dict) else {}
        builtin = builtin_profile_refs(config) | {BUILTIN_PROFILE_REF}
        sources = {}
        for item in config.get("devices") or []:
            if not is_external_mqtt_device_config(item) or not config_entry_enabled(item):
                continue
            if zendure_mqtt_topic_family(item) not in EXTERNAL_TOPIC_FAMILIES:
                continue
            ref, device_id = zendure_mqtt_broker_ref(item), zendure_mqtt_route_device_id(item)
            profile = profiles.get(ref) if isinstance(profiles.get(ref), dict) else {}
            where = BUILTIN_IDENTITY if ref in builtin else identity(profile.get("host"), profile.get("port", DEFAULT_PORT))
            if device_id and where:
                sources.setdefault(device_id, {}).setdefault(where, set()).add("configured")
        for candidate in self._discovery.candidates():
            where = identity(candidate.get("host"), candidate.get("port"))
            for device in candidate.get("devices") or []:
                if where and device.get("topic_family") in EXTERNAL_TOPIC_FAMILIES and device.get("device_id"):
                    sources.setdefault(device["device_id"], {}).setdefault(where, set()).add("published")
        return sources

    def elsewhere(self, broker, device_id, broker_ref, *, address=None):
        """A broker other than ``broker_ref``'s that publishes ``device_id`` without the EMS reading it there, or ``None``.

        The broker an EMS device was added through does not count, published
        or not: moving a mapping away from it is how a device changes broker,
        Find devices then offers Use this connection, and its old copy expires.
        """

        mine = self.ref_identity(broker, broker_ref, address=address)
        others = sorted(
            where
            for where, kinds in self.external_sources(broker, address=address).get(device_id, {}).items()
            if where != mine and "published" in kinds and "configured" not in kinds
        )
        return others[0] if others else None

    def taken_ids(self, broker, broker_ref, *, address=None) -> list[str]:
        """The catalog ids another broker than ``broker_ref``'s has, so a new mapping there is never offered one."""

        mine = self.ref_identity(broker, broker_ref, address=address)
        sources = self.external_sources(broker, address=address)
        return sorted(device_id for device_id, places in sources.items() if set(places) - {mine})

    # --- checking, searching, adding --------------------------------------

    def _pool(self, first=None):
        refs = [first] if first else []
        with contextlib.suppress(Exception):
            refs += list(self._preparation.load()["local_mqtt"]["credential_refs"])
        seen = set()
        for ref in refs:
            if not ref or ref in seen:
                continue
            seen.add(ref)
            secret = self._store.load_mqtt_discovery_secret(ref)
            if secret is not None and secret.username:
                yield ref, secret

    def _check(self, item):
        """Its own account first, if it has one; then anonymous; then each account of the pool."""

        base = {"host": item.host, "port": item.port, "tls": item.tls, "tls_insecure": item.tls_insecure}
        own = next(iter(self._pool_only(item.credentials_ref)), None)
        if own is not None and self._login({**base, "username": own[1].username, "password": own[1].password}) == "accepted":
            return replace(item, access="account", credentials_ref=own[0])
        state = self._login(base)
        if state == "accepted":
            return replace(item, access="anonymous", credentials_ref=None)
        if state in ("unreachable", "untrusted"):
            return replace(item, access=state)
        for ref, secret in self._pool(item.credentials_ref):
            if self._login({**base, "username": secret.username, "password": secret.password}) == "accepted":
                return replace(item, access="account", credentials_ref=ref)
        return replace(item, access="needs_account", credentials_ref=None)

    def _pool_only(self, ref):
        if not ref:
            return []
        secret = self._store.load_mqtt_discovery_secret(ref)
        return [(ref, secret)] if secret is not None and secret.username else []

    def _store_checked(self, items):
        checked = {}
        if items:
            with ThreadPoolExecutor(max_workers=min(self._workers, len(items))) as pool:
                checked = {item.ref: result for item, result in zip(items, pool.map(self._check, items))}
        with self._lock:
            self._found.update(checked)

    def _saved_item(self, broker, ref):
        entry = saved_brokers(read_mappings_document(mappings_path(broker.config_dir))).get(ref) if broker else None
        if entry is None:
            return None
        return FoundBroker(ref, entry["host"], entry["port"], entry["tls"], entry["tls_insecure"], credentials_ref=entry["credentials_ref"], origin="saved")

    def _remember_account(self, broker, ref, credentials_ref):
        """Point a remembered broker at the account that works now, and let the mapping read with it."""

        path = mappings_path(broker.config_dir)
        document = read_mappings_document(path)
        entry = (document or {}).get("brokers", {}).get(ref)
        if isinstance(entry, dict) and entry.get("credentials_ref") != credentials_ref:
            entry["credentials_ref"] = credentials_ref
            atomic_write_bytes(path, json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
        broker.sync_mapping_container()

    def check(self, ref, broker=None) -> dict:
        """Check a broker again; a remembered one keeps the account the check settles on."""

        saved = self._saved_item(broker, ref)
        if saved is not None:
            result = self._check(saved)
            if result.usable:
                self._remember_account(broker, ref, result.credentials_ref)
            self._note_saved(broker, ref, result.access)
            return {"ok": True}
        with self._lock:
            item = self._found.get(ref)
        if item is None:
            return {"ok": False, "error": "mqtt_mapping_broker_unknown"}
        self._store_checked([item])
        return {"ok": True}

    def search(self, broker, *, address=None) -> dict:
        """Look for brokers on every network the host recommends, then check each one found."""

        if not self._searching.acquire(blocking=False):
            return {"ok": False, "error": "mqtt_mapping_broker_search_busy"}
        try:
            networks = []
            with contextlib.suppress(Exception):
                networks = [
                    item["cidr"]
                    for item in self._networks().get("networks") or []
                    if item.get("scan_recommended") and item.get("cidr")
                ]
            for cidr in networks:
                with contextlib.suppress(Exception):
                    self._discovery.probe(cidr)
            visible = {item["ref"] for item in self.choices(broker, address=address)["brokers"] if item["kind"] == "found"}
            with self._lock:
                pending = [item for ref, item in self._found.items() if ref in visible]
            self._store_checked(pending)
            return {"ok": True, "networks": networks, "found": len(pending)}
        finally:
            self._searching.release()

    def use_account(self, ref, username, password, broker=None) -> dict:
        """Check an account on a found or remembered broker; keep it in the discovery pool when the broker accepts it."""

        if _account_problem(username, password):
            return {"ok": False, "error": "mqtt_mapping_broker_account_invalid"}
        saved = self._saved_item(broker, ref)
        with self._lock:
            item = saved or self._found.get(ref)
        if item is None:
            return {"ok": False, "error": "mqtt_mapping_broker_unknown"}
        endpoint = {"host": item.host, "port": item.port, "tls": item.tls, "tls_insecure": item.tls_insecure}
        state = self._login({**endpoint, "username": username, "password": password})
        if state != "accepted":
            return {"ok": False, "error": f"mqtt_mapping_broker_{state}"}
        self._store.save_mqtt_discovery_secret(ref, username, password, label=f"{item.host}:{item.port}")
        self._preparation.add_credential_ref(ref)
        if saved is not None:
            self._remember_account(broker, ref, ref)
            self._note_saved(broker, ref, "account")
            return {"ok": True, "ref": ref}
        with self._lock:
            self._found[ref] = replace(item, access="account", credentials_ref=ref)
        return {"ok": True, "ref": ref}

    def add_manual(self, host, port, *, tls=False, tls_insecure=False, username=None, password=None) -> dict:
        """A broker the search cannot reach on its own, named by the operator and checked at once."""

        host = host.strip() if isinstance(host, str) else ""
        valid_port = isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535
        if not valid_host(host) or not valid_port or not isinstance(tls, bool) or not isinstance(tls_insecure, bool):
            return {"ok": False, "error": "mqtt_mapping_broker_endpoint_invalid"}
        ref = found_ref(host, port)
        item = FoundBroker(ref, host, port, tls=tls, tls_insecure=tls and tls_insecure, origin="manual")
        with self._lock:
            self._found[ref] = item
        if username:
            return self.use_account(ref, username, password or "")
        self._store_checked([item])
        return {"ok": True, "ref": ref}


def seed_saved_brokers(discovery, config_dir):
    """Let Find devices listen on every broker a mapping remembers, so its published devices can be added."""

    brokers = [
        {
            "id": f"mqtt:{entry['host']}:{entry['port']}",
            "host": entry["host"],
            "port": entry["port"],
            "label": ref,
            "tls": entry["tls"],
            "tls_insecure": entry["tls_insecure"],
            "credentials_ref": entry["credentials_ref"],
        }
        for ref, entry in saved_brokers(read_mappings_document(mappings_path(config_dir))).items()
    ]
    with contextlib.suppress(Exception):
        discovery.set_configured_brokers(brokers)


def seed_builtin_broker(discovery, broker, address):
    """Let Find devices listen on the built-in broker while it is switched on, also after the Admin restarts."""

    with contextlib.suppress(Exception):
        settings, _issues, _exists = broker.load()
        if address and settings is not None and settings.enabled:
            discovery.set_configured_brokers(
                [{"id": "mqtt:builtin", "host": address, "port": settings.port, "label": "Built-in MQTT broker"}]
            )


__all__ = [
    "ACCESS_STATES",
    "ERROR_CODES",
    "FoundBroker",
    "MappingBrokers",
    "found_ref",
    "paho_login",
    "seed_builtin_broker",
    "seed_saved_brokers",
]
