# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live mDNS discovery provider for admin discovery.

A background provider that browses EMS-relevant mDNS services, HTTP-verifies
each candidate with the same probes the network scan uses, and merges verified
devices into an in-memory store that the UI polls. It is its own discovery
source: it always merges and never clears manual scan results.

The ``zeroconf`` library is optional. When it is missing, the provider degrades
to an ``unavailable_dependency`` status instead of raising, and everything else
keeps working. All mDNS-derived values
(service name, hostname, TXT) are untrusted and are treated as data only.
"""

import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from admin.discovery import verify_shelly_meter_endpoint, verify_zendure_endpoint
from admin.models import utc_now_iso
from admin.mqtt_discovery import MQTT_MDNS_SERVICE_TYPE, build_mqtt_mdns_candidate

HTTP_MDNS_SERVICE_TYPE = "_http._tcp.local."
ZENDURE_MDNS_SERVICE_TYPE = "_zendure._tcp.local."
SHELLY_MDNS_SERVICE_TYPE = "_shelly._tcp.local."
MDNS_SERVICE_TYPES = (
    HTTP_MDNS_SERVICE_TYPE,
    ZENDURE_MDNS_SERVICE_TYPE,
    SHELLY_MDNS_SERVICE_TYPE,
)

# Stale thresholds (seconds). A device stays visible until the user clears
# results; these only drive the recent/stale/old marker.
RECENT_SECONDS = 120
STALE_SECONDS = 600

VERIFY_MAX_WORKERS = 4
VERIFY_TIMEOUT_MS = 5000

UNVERIFIED_REASON = "mDNS service found, HTTP verification pending or failed"
NO_ADDRESS_REASON = "mDNS service has no resolvable IP address"
UNKNOWN_SERVICE_REASON = "mDNS service is not a supported EMS device."
UNSUPPORTED_SHELLY_REASON = (
    "Shelly device discovered by mDNS, but it is not a supported EMS grid meter."
)


def decode_txt(properties):
    """Decode a zeroconf TXT ``properties`` mapping (bytes) into plain strings."""

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


def _service_instance_name(service_name, service_type):
    name = str(service_name or "").rstrip(".")
    suffix = "." + str(service_type or "").rstrip(".")
    if name.lower().endswith(suffix.lower()):
        return name[:-len(suffix)]
    return name


def classify_zendure_service(service_name, service_type):
    """Return Zendure hints for a supported service, otherwise ``None``."""

    instance_name = _service_instance_name(service_name, service_type)
    is_zendure_name = instance_name.lower().startswith("zendure-")
    if service_type not in (HTTP_MDNS_SERVICE_TYPE, ZENDURE_MDNS_SERVICE_TYPE):
        return None
    if service_type == HTTP_MDNS_SERVICE_TYPE and not is_zendure_name:
        return None

    hints = {"vendor": "Zendure"}
    if not is_zendure_name:
        return hints

    advertised_name = instance_name[len("Zendure-"):]
    model_hint, separator, serial_hint = advertised_name.rpartition("-")
    if separator and model_hint:
        hints["model_hint"] = model_hint
        if serial_hint:
            hints["serial_number_hint"] = serial_hint
    elif advertised_name:
        hints["model_hint"] = advertised_name
    return hints


def classify_mdns_service(service_name, service_type):
    """Return vendor/model hints for a candidate on a browsed service type."""

    zendure = classify_zendure_service(service_name, service_type)
    if zendure is not None:
        return zendure
    instance_name = _service_instance_name(service_name, service_type)
    if (
        service_type == SHELLY_MDNS_SERVICE_TYPE
        or (
            service_type == HTTP_MDNS_SERVICE_TYPE
            and instance_name.lower().startswith("shelly")
        )
    ):
        hints = {"vendor": "Shelly"}
        model_hint = instance_name.split("-", 1)[0]
        if model_hint:
            hints["model_hint"] = model_hint
        return hints
    return {}


def build_candidate(service_name, hostname, addresses, port, properties,
                    service_type=ZENDURE_MDNS_SERVICE_TYPE):
    """Normalize a browsed mDNS service into an untrusted candidate dict."""

    if service_type not in MDNS_SERVICE_TYPES:
        return None
    hints = classify_mdns_service(service_name, service_type)
    ip = addresses[0] if addresses else None
    candidate = {
        "source": "mdns",
        "service_name": service_name,
        "hostname": hostname,
        "ip": ip,
        "port": int(port) if port else 80,
        "txt": decode_txt(properties),
        "service_type": service_type,
    }
    candidate.update(hints)
    return candidate


def _fallback_id(candidate):
    name = candidate.get("service_name") or candidate.get("hostname")
    if name:
        return f"mdns:{name}"
    return f"mdns:{candidate.get('ip')}:{candidate.get('port')}:{candidate.get('service_type')}"


def _base_entry(candidate):
    return {
        "source": "mdns",
        "source_detail": candidate.get("service_type"),
        "vendor": candidate.get("vendor"),
        "model_hint": candidate.get("model_hint"),
        "serial_number_hint": candidate.get("serial_number_hint"),
        "ip": candidate.get("ip"),
        "port": candidate.get("port", 80),
        "protocol": "http",
        "service_name": candidate.get("service_name"),
        "hostname": candidate.get("hostname"),
        "sources": ["mdns"],
        "last_seen": candidate.get("_last_seen") or utc_now_iso(),
        "last_verify_attempt": utc_now_iso(),
    }


def _unverified_entry(candidate, reason):
    entry = _base_entry(candidate)
    entry.update({
        "id": _fallback_id(candidate),
        "display_name": (
            f"{candidate.get('vendor')} mDNS candidate"
            if candidate.get("vendor") else "mDNS candidate"
        ),
        "confidence": 0.45,
        "verified": False,
        "usable_for_config": False,
        "reason": reason,
    })
    return entry


def _verified_entry(candidate, device):
    entry = device.to_dict()
    entry.update(_base_entry(candidate))
    entry.update({
        "verified": True,
        "usable_for_config": device.config_ready,
        # Prefer the mDNS-advertised port; fall back to the verified device port.
        "port": candidate.get("port") or device.port,
    })
    return entry


def _default_verifier(candidate):
    failures = []
    if candidate.get("vendor") == "Zendure":
        device = verify_zendure_endpoint(
            candidate["ip"], candidate.get("port", 80),
            timeout_ms=VERIFY_TIMEOUT_MS, failure_details=failures,
        )
        return device, failures[-1] if failures else None
    if candidate.get("vendor") == "Shelly":
        device = verify_shelly_meter_endpoint(
            candidate["ip"], candidate.get("port", 80),
            timeout_ms=VERIFY_TIMEOUT_MS, failure_details=failures,
        )
        return device, failures[-1] if failures else None
    return None, None


def verify_candidate(candidate, verifier=None):
    """Verify one mDNS candidate over HTTP; return a verified or unverified entry."""

    ip = candidate.get("ip")
    if not ip:
        return _unverified_entry(candidate, NO_ADDRESS_REASON)
    vendor = candidate.get("vendor")
    if vendor not in ("Zendure", "Shelly"):
        return _unverified_entry(candidate, UNKNOWN_SERVICE_REASON)
    result = (
        verifier(ip, candidate.get("port", 80))
        if verifier is not None else _default_verifier(candidate)
    )
    device, failure_reason = (
        result if isinstance(result, tuple) and len(result) == 2 else (result, None)
    )
    if device is None:
        reason = failure_reason or (
            UNSUPPORTED_SHELLY_REASON if vendor == "Shelly" else UNVERIFIED_REASON
        )
        return _unverified_entry(candidate, reason)
    if vendor == "Shelly" and not (
        device.api_family in ("shelly_gen2", "shelly_3em_gen1")
        and device.role_suggestion == "grid_meter"
        and device.config_ready
    ):
        return _unverified_entry(candidate, UNSUPPORTED_SHELLY_REASON)
    return _verified_entry(candidate, device)


def _union_sources(*lists):
    seen = []
    for source_list in lists:
        for source in source_list or []:
            if source not in seen:
                seen.append(source)
    return seen


def merge_entries(existing, incoming):
    """Merge a rediscovered device into an existing entry (see task merge rules)."""

    merged = dict(existing)
    for key, value in incoming.items():
        if value is not None:
            merged[key] = value
    merged["sources"] = _union_sources(existing.get("sources"), incoming.get("sources"))
    merged["verified"] = bool(existing.get("verified")) or bool(incoming.get("verified"))
    merged["usable_for_config"] = (
        bool(existing.get("usable_for_config")) or bool(incoming.get("usable_for_config"))
    )
    if merged["verified"]:
        merged.pop("reason", None)
    return merged


def _age_seconds(last_seen, now):
    if not last_seen:
        return None
    try:
        seen = datetime.strptime(last_seen, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return max(0.0, (now - seen).total_seconds())


def _stale_level(age):
    if age is None:
        return "unknown"
    if age <= RECENT_SECONDS:
        return "recent"
    if age <= STALE_SECONDS:
        return "stale"
    return "old"


class DeviceStore:
    """Thread-safe store of mDNS-discovered devices, keyed by stable id."""

    def __init__(self):
        self._lock = threading.Lock()
        self._devices = {}

    def merge(self, entry, replace_key=None):
        key = entry.get("id") or _fallback_id(entry)
        with self._lock:
            existing = self._devices.get(key)
            self._devices[key] = merge_entries(existing, entry) if existing else entry
            if replace_key and replace_key != key:
                self._devices.pop(replace_key, None)
        return key

    def touch(self, key):
        """Mark an unchanged advertised service as recently seen."""

        with self._lock:
            if key not in self._devices:
                return False
            self._devices[key]["last_seen"] = utc_now_iso()
            return True

    def is_verified(self, key):
        with self._lock:
            entry = self._devices.get(key)
            return bool(entry and entry.get("verified"))

    def to_list(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self._lock:
            entries = [dict(entry) for entry in self._devices.values()]
        for entry in entries:
            age = _age_seconds(entry.get("last_seen"), now)
            entry["stale"] = age is None or age > RECENT_SECONDS
            entry["stale_level"] = _stale_level(age)
        return entries

    def clear(self):
        with self._lock:
            self._devices.clear()


class MdnsProvider:
    """mDNS discovery lifecycle: browse, verify, merge, expose status.

    ``browser_factory`` (``(service_type, handler) -> browser``) is injected in
    tests so the lifecycle can be exercised without real multicast. In production
    it is ``None`` and ``zeroconf`` is imported lazily inside :meth:`start`.
    """

    def __init__(self, verifier=None, store=None, browser_factory=None,
                 mqtt_handler=None):
        self._verifier = verifier
        self._store = store or DeviceStore()
        self._browser_factory = browser_factory
        self._mqtt_handler = mqtt_handler
        self._lock = threading.Lock()
        self._enabled = False
        self._started = False
        self._last_event = None
        self._last_error = None
        self._last_refresh = None
        self._startup_failed = False
        self._zc = None
        self._browsers = []
        self._executor = None
        self._candidate_cache = {}
        self._known_candidates = {}

    @property
    def available(self):
        if self._browser_factory is not None:
            return True
        return importlib.util.find_spec("zeroconf") is not None

    def _state(self, verified_count):
        if not self.available:
            return "unavailable_dependency"
        if self._startup_failed:
            return "unavailable_runtime"
        if not self._enabled:
            return "disabled"
        return "running_with_devices" if verified_count else "running_no_devices"

    def status(self):
        devices = self._store.to_list()
        verified = [d for d in devices if d.get("verified")]
        state = self._state(len(verified))
        messages = {
            "running_with_devices": (
                f"Automatic mDNS discovery found {len(verified)} supported "
                f"{'device' if len(verified) == 1 else 'devices'}."
            ),
            "running_no_devices": (
                "Automatic mDNS discovery is running. No supported devices found yet."
            ),
            "unavailable_dependency": (
                "Automatic mDNS discovery is unavailable because zeroconf "
                "is not installed."
            ),
            "unavailable_runtime": (
                "Automatic mDNS discovery is unavailable because mDNS could "
                "not be started."
            ),
            "disabled": "Automatic mDNS discovery is disabled.",
        }
        return {
            "enabled": self._enabled,
            "available": self.available,
            "running": self._enabled and self._started,
            "state": state,
            "message": messages[state],
            "last_event": self._last_event,
            "last_error": self._last_error,
            "last_refresh": self._last_refresh,
            "verified_count": len(verified),
            "ignored_count": len(devices) - len(verified),
            "mdns_device_count": len(verified),
            "network_hint": (
                "mDNS may require host networking or multicast support to see LAN devices."
            ),
        }

    def devices(self):
        return [d for d in self._store.to_list() if d.get("verified")]

    def ignored_devices(self):
        return [d for d in self._store.to_list() if not d.get("verified")]

    def handle_candidate(self, candidate, force_verify=False):
        """Entry point for a resolved mDNS service (verify + merge, off-thread)."""

        if not candidate:
            return
        if not force_verify:
            self._last_event = utc_now_iso()
            candidate = dict(candidate)
            candidate["_last_seen"] = self._last_event
        identity = _fallback_id(candidate)
        signature = (
            candidate.get("ip"),
            candidate.get("port"),
            candidate.get("hostname"),
            tuple(sorted((candidate.get("txt") or {}).items())),
        )
        with self._lock:
            if not force_verify:
                self._known_candidates[identity] = dict(candidate)
            cached = self._candidate_cache.get(identity)
        if (
            not force_verify
            and cached
            and cached[0] == signature
            and self._store.touch(cached[1])
        ):
            return
        if self._executor is not None:
            return self._executor.submit(
                self._verify_and_merge, identity, signature, candidate
            )
        else:
            return self._verify_and_merge(identity, signature, candidate)

    def _verify_and_merge(self, identity, signature, candidate):
        try:
            with self._lock:
                cached = self._candidate_cache.get(identity)
            old_key = cached[1] if cached else None
            replace_key = (
                old_key if old_key and not self._store.is_verified(old_key) else None
            )
            key = self._store.merge(
                verify_candidate(candidate, self._verifier),
                replace_key=replace_key,
            )
            with self._lock:
                self._candidate_cache[identity] = (signature, key)
        except Exception as exc:  # keep the listener alive; surface as status text
            self._last_error = str(exc)

    def start(self):
        if not self.available:
            self._last_error = "mDNS library (zeroconf) is not installed"
            self._startup_failed = True
            return self.status()
        with self._lock:
            if self._started:
                self._enabled = True
                return self.status()
            try:
                self._executor = ThreadPoolExecutor(max_workers=VERIFY_MAX_WORKERS)
                self._make_browsers()
                self._started = True
                self._enabled = True
                self._last_error = None
                self._startup_failed = False
            except Exception as exc:
                self._last_error = f"could not start mDNS discovery: {exc}"
                self._startup_failed = True
                self._teardown_locked()
        return self.status()

    def stop(self):
        with self._lock:
            self._enabled = False
            self._startup_failed = False
            self._teardown_locked()
        return self.status()

    enable = start
    disable = stop

    def refresh(self):
        """Restart browsing and retry every known unverified candidate."""

        if not self._enabled or not self._started:
            status = self.start()
            if not status.get("running"):
                return self.status()
        else:
            with self._lock:
                try:
                    self._cancel_browsers_locked()
                    if self._zc is not None:
                        self._zc.close()
                        self._zc = None
                    self._make_browsers()
                    self._last_error = None
                except Exception as exc:
                    self._last_error = f"could not refresh mDNS discovery: {exc}"
                    self._startup_failed = True
                    self._teardown_locked()
                    return self.status()

        with self._lock:
            retry_candidates = [
                dict(candidate)
                for identity, candidate in self._known_candidates.items()
                if not (
                    self._candidate_cache.get(identity)
                    and self._store.is_verified(
                        self._candidate_cache[identity][1]
                    )
                )
            ]
        futures = [
            future
            for future in (
                self.handle_candidate(candidate, force_verify=True)
                for candidate in retry_candidates
            )
            if future is not None
        ]
        for future in futures:
            future.result()
        self._last_refresh = utc_now_iso()
        return self.status()

    def _cancel_browsers_locked(self):
        for browser in self._browsers:
            try:
                browser.cancel()
            except Exception:
                pass
        self._browsers = []

    def _teardown_locked(self):
        self._cancel_browsers_locked()
        for closer in (
            lambda: self._zc and self._zc.close(),
            lambda: self._executor and self._executor.shutdown(wait=False),
        ):
            try:
                closer()
            except Exception:
                pass
        self._zc = None
        self._executor = None
        self._started = False

    def _make_browsers(self):
        self._browsers = []
        if self._browser_factory is not None:
            for service_type in MDNS_SERVICE_TYPES:
                self._browsers.append(
                    self._browser_factory(service_type, self.handle_candidate)
                )
            if self._mqtt_handler is not None:
                self._browsers.append(
                    self._browser_factory(MQTT_MDNS_SERVICE_TYPE, self._mqtt_handler)
                )
            return
        from zeroconf import ServiceBrowser, Zeroconf

        self._zc = Zeroconf()
        for service_type in MDNS_SERVICE_TYPES:
            listener = _ZeroconfListener(
                self._zc, service_type, self.handle_candidate
            )
            self._browsers.append(
                ServiceBrowser(self._zc, service_type, listener)
            )
        if self._mqtt_handler is not None:
            listener = _ZeroconfListener(
                self._zc,
                MQTT_MDNS_SERVICE_TYPE,
                self._mqtt_handler,
                candidate_builder=build_mqtt_mdns_candidate,
            )
            self._browsers.append(
                ServiceBrowser(self._zc, MQTT_MDNS_SERVICE_TYPE, listener)
            )


class _ZeroconfListener:
    """Adapter that resolves zeroconf service events into candidate dicts."""

    def __init__(self, zeroconf, service_type, handler,
                 candidate_builder=build_candidate):
        self._zc = zeroconf
        self._service_type = service_type
        self._handler = handler
        self._candidate_builder = candidate_builder

    def add_service(self, zeroconf, service_type, name):
        self._resolve(zeroconf, service_type, name)

    def update_service(self, zeroconf, service_type, name):
        self._resolve(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type, name):
        # Devices are kept until the user clears results; only the stale marker moves.
        return

    def _resolve(self, zeroconf, service_type, name):
        info = zeroconf.get_service_info(service_type, name, timeout=2000)
        if info is None:
            return
        try:
            addresses = info.parsed_addresses()
        except Exception:
            addresses = []
        fields = {
            "service_name": name,
            "hostname": info.server,
            "addresses": addresses,
            "port": info.port,
            "properties": getattr(info, "properties", {}),
        }
        if self._candidate_builder is build_candidate:
            fields["service_type"] = service_type
        candidate = self._candidate_builder(**fields)
        if candidate is not None:
            self._handler(candidate)
