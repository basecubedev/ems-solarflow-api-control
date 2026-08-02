# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin-only read-only discovery against the Zendure cloud MQTT broker.

Flow: saved Zendure API key or HA token -> deviceList (real hardware) ->
encrypted TLS MQTT listen -> enrich candidates with observed topics.
Discovery-display-only:

* never publishes, never issues properties/read|write or function/invoke,
* never writes the EMS config,
* always connects with TLS and never falls back to plaintext,
* never returns or logs the raw API key/token, MQTT password/username, product
  key, device key, or serial in error text.

The listener is injectable so the whole flow is testable without a real broker.
"""

import time
from dataclasses import dataclass

from admin.models import (
    DISCOVERY_DEVICE_LIST_ONLY,
    DISCOVERY_MQTT_OBSERVED,
    MqttHardwareCandidate,
    SOURCE_ZENDURE_CLOUD_MQTT,
    utc_now_iso,
)
from admin.mqtt_topic_discovery import (
    FAMILY_UNKNOWN,
    MAX_METRICS_PER_CANDIDATE,
    MAX_TOPICS_PER_CANDIDATE,
    _JSON_FAMILIES,
    _score,
    classify_topic,
    parse_report_payload,
)
from admin.secret_store import ZendureTokenStore
from ems.config import MQTT_TLS_OBSERVED_MODES
from admin.zendure_cloud_auth import (
    ZendureCloudError,
    fetch_device_list,
    normalize_app_key,
    resolve_device_list_credential,
)

TLS_SYSTEM_CA = "system_ca"
TLS_PINNED_CA = "pinned_ca"
TLS_ENCRYPTED_NO_VERIFY = "encrypted_no_verify"
# The accepted set is Core's; these names only select the CA strategy for the
# discovery client's own connection.
TLS_MODES = tuple(sorted(MQTT_TLS_OBSERVED_MODES))

SOURCE_LABEL = "Zendure MQTT discovery"
AUTH_MODE_TOKEN = "api_token"

# Supported Zendure cloud credential shapes. The UI has one auto-detecting
# input; these names remain for API compatibility with older clients.
CREDENTIAL_MODE_API_KEY = "zendure_api_key"
CREDENTIAL_MODE_HA_TOKEN = "ha_device_list_token"

# Effective deviceList timeout. The live endpoint can take ~14-15s to respond,
# so this is well above the MQTT-listen window below (they are independent).
DEVICE_LIST_TIMEOUT_S = 25.0
# Minimum gap between best-effort lazy deviceList seedings, so an empty cache
# does not trigger a slow deviceList call on every discovery run.
SEED_COOLDOWN_S = 60.0

DEFAULT_CLOUD_TIMEOUT_S = 8.0
# Total budget for the pre-connect reachability check. The Zendure broker
# resolves to several IPs; without this bound an unreachable 8883 makes paho
# retry each address and block for tens of seconds.
CONNECT_TIMEOUT_S = 5.0
MAX_CLOUD_TIMEOUT_S = 12.0
MAX_CLOUD_DEVICES = 32
MAX_CLOUD_SUBSCRIPTIONS = 96
MAX_CLOUD_TOPICS = 400
MAX_CLOUD_CANDIDATES = 48
DEVICE_LIST_CONFIDENCE = 0.5

# The Zendure deviceList usually returns the broker URL without a port, and the
# raw parse then defaults to 1883 (the *plaintext* MQTT port). Cloud discovery is
# TLS-only, so the effective connection falls back to 8883 (MQTT-over-TLS).
DEFAULT_TLS_MQTT_PORT = 8883
PLAINTEXT_MQTT_PORT = 1883

_ERR_NOT_CONFIGURED = (
    "Save a Zendure API key or HA/deviceList token to discover cloud devices."
)
_ERR_MQTT_FAILED = "TLS connection to Zendure MQTT broker failed."
_ERR_NO_TELEMETRY = "No MQTT telemetry was observed within the discovery window."
_ERR_UNSUPPORTED_CREDENTIAL_MODE = (
    "Use a Zendure API key or HA/deviceList token for Zendure MQTT discovery."
)


def credential_mode_is_supported(mode):
    """True for omitted/auto mode or either supported Zendure credential."""

    text = str(mode or "").strip().lower()
    return text in ("", CREDENTIAL_MODE_API_KEY, CREDENTIAL_MODE_HA_TOKEN)


class CloudMqttError(Exception):
    """Redaction-safe cloud MQTT listener error (never carries secrets)."""


@dataclass
class CloudMqttConnection:
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    mqtt_protocol: str = "3.1"
    subscriptions: tuple = ()
    tls_enabled: bool = True
    tls_mode: str = TLS_SYSTEM_CA
    ca_cert: str | None = None


def mask_id(value):
    """Redact a secret-ish id (appKey/productKey/deviceKey) for display/logs."""

    text = str(value or "")
    if not text:
        return None
    if len(text) <= 4:
        return "••••"
    return "…" + text[-4:]


def _redact_topic(topic, sensitive):
    text = str(topic or "")
    for token in sensitive:
        if token:
            text = text.replace(token, "•••")
    return text


# --- MQTT listeners ------------------------------------------------------


class FakeCloudMqttListener:
    """Replays fixed ``(topic, payload)`` messages; records TLS/connect setup.

    Used by tests to prove the cloud listener is TLS-configured, receives the
    credentials, and never publishes, without any real broker.
    """

    def __init__(self, connection, messages=(), *, fail=False):
        self.connection = connection
        self._messages = list(messages)
        self._fail = fail
        self.published = []
        self.tls_configured = False
        self.connected = False

    def listen(self, timeout_s, on_message):
        if not self.connection.tls_enabled:
            raise CloudMqttError(_ERR_MQTT_FAILED)
        self.tls_configured = True
        if self._fail:
            raise CloudMqttError(_ERR_MQTT_FAILED)
        self.connected = True
        for topic, payload in self._messages:
            on_message(topic, payload)

    def publish(self, *args, **kwargs):  # pragma: no cover - guard only
        self.published.append((args, kwargs))


class PahoTlsMqttListener:
    """Authenticated, TLS-only, read-only paho listener bounded by a timeout.

    Applies ``tls_set`` before ``connect`` and never publishes. Plaintext is
    refused outright; there is no downgrade path. ``client_factory`` is
    injectable so the TLS/auth setup can be asserted without a real broker.
    """

    def __init__(self, connection, *, client_factory=None):
        self.connection = connection
        self._client_factory = client_factory
        self.published = []

    def listen(self, timeout_s, on_message):
        if not self.connection.tls_enabled:
            raise CloudMqttError(_ERR_MQTT_FAILED)
        # Real runs (no injected client factory) fail fast on an unreachable
        # broker instead of letting paho block per resolved IP.
        if self._client_factory is None:
            self._preflight_reachable()
        client = self._make_client()
        try:
            self._configure_tls(client)
            if self.connection.username is not None:
                client.username_pw_set(
                    self.connection.username, self.connection.password
                )

            def _on_connect(client_, *_args, **_kwargs):
                for topic in self.connection.subscriptions:
                    client_.subscribe(topic, qos=0)

            def _on_message(_client, _userdata, message):
                on_message(message.topic, message.payload)

            client.on_connect = _on_connect
            client.on_message = _on_message
            client.connect(
                self.connection.host,
                self.connection.port,
                keepalive=max(2, int(timeout_s) + 2),
            )
            client.loop_start()
            try:
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    time.sleep(0.1)
            finally:
                client.loop_stop()
                try:
                    client.disconnect()
                except Exception:
                    pass
        except CloudMqttError:
            raise
        except Exception as exc:  # never surface raw broker/TLS/secret detail
            raise CloudMqttError(_ERR_MQTT_FAILED) from exc

    def _preflight_reachable(self):
        import socket

        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        last_exc = None
        try:
            infos = socket.getaddrinfo(
                self.connection.host, self.connection.port, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise CloudMqttError(_ERR_MQTT_FAILED) from exc
        for family, socktype, proto, _canon, sockaddr in infos:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(remaining)
            try:
                sock.connect(sockaddr)
                return
            except OSError as exc:
                last_exc = exc
            finally:
                sock.close()
        raise CloudMqttError(_ERR_MQTT_FAILED) from last_exc

    def _make_client(self):
        if self._client_factory is not None:
            return self._client_factory(self.connection)
        import paho.mqtt.client as mqtt

        protocol = {
            "3.1": mqtt.MQTTv31,
            "3.1.1": mqtt.MQTTv311,
            "5": mqtt.MQTTv5,
        }.get(str(self.connection.mqtt_protocol), mqtt.MQTTv31)
        client_id = self.connection.client_id or ""
        try:
            return mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                protocol=protocol,
            )
        except (AttributeError, TypeError):  # paho < 2.0 has no versioned ctor
            return mqtt.Client(client_id=client_id, protocol=protocol)

    def _configure_tls(self, client):
        from ems.config import configure_mqtt_client_tls

        mode = self.connection.tls_mode
        configure_mqtt_client_tls(
            client,
            tls=True,
            tls_insecure=mode == TLS_ENCRYPTED_NO_VERIFY,
            ca_certs=(
                self.connection.ca_cert if mode == TLS_PINNED_CA else None
            ),
        )


def default_cloud_listener_factory(connection):
    return PahoTlsMqttListener(connection)


# --- candidate aggregation ----------------------------------------------


class CloudCandidateSet:
    """Seeds deviceList candidates and enriches them with observed topics.

    Internal matching keeps the raw product/device keys; the public dicts mask
    them (and redact topics) so no account-scoped secret leaves the process.
    """

    def __init__(self, broker_host, broker_port, tls_mode, app_key=None):
        self.broker_id = f"zendure-cloud:{broker_host}"
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.tls_mode = tls_mode
        self._app_key = app_key
        self._candidates = []
        self._by_serial = {}
        self._by_device_key = {}
        self._by_pk_dk = {}
        # Raw product keys are write-target identities. They stay in this
        # process-only map and are exposed solely through ``trusted_results``;
        # every browser/discovery response continues to use masked ``results``.
        self._trusted_product_keys = {}
        # The MQTT routing id is the authenticated deviceList ``deviceKey`` (or
        # the equivalent id observed on a topic), not the physical serial. For
        # JSON-report devices these values may differ. Keep it behind the same
        # trusted boundary as product keys so it never reaches discovery HTTP
        # responses in clear text.
        self._trusted_route_ids = {}
        self._pack_seen = set()  # ids of candidates that reported battery-pack data
        self._topic_count = 0
        self._sensitive = {app_key} if app_key else set()

    def seed_devices(self, devices):
        for entry in devices[:MAX_CLOUD_DEVICES]:
            self._seed_device(entry)

    def _seed_device(self, entry):
        serial = entry.get("snNumber")
        device_key = entry.get("deviceKey")
        product_key = entry.get("productKey")
        model = entry.get("productModel")
        name = entry.get("deviceName")
        candidate = MqttHardwareCandidate(
            broker_id=self.broker_id,
            broker_host=self.broker_host,
            broker_port=self.broker_port,
            topic_family=DISCOVERY_DEVICE_LIST_ONLY,
            device_id=serial or mask_id(device_key),
            serial_number=serial,
            model_hint=model,
            display_name=name or model or "Zendure cloud device",
            confidence=DEVICE_LIST_CONFIDENCE,
            source_type=SOURCE_ZENDURE_CLOUD_MQTT,
            source_label=SOURCE_LABEL,
            broker_label=f"{self.broker_host}:{self.broker_port}",
            discovery_status=DISCOVERY_DEVICE_LIST_ONLY,
            product_key=mask_id(product_key),
            device_key=mask_id(device_key),
            device_name=name,
            tls_mode=self.tls_mode,
            auth_mode=AUTH_MODE_TOKEN,
        )
        self._candidates.append(candidate)
        if product_key:
            self._trusted_product_keys[id(candidate)] = product_key
        if device_key:
            # deviceList is fetched with the saved account credential and is
            # already the authority used to build the exact cloud subscriptions.
            # Telemetry can be quiet for longer than the bounded discovery
            # window, so waiting for a matching MQTT message here would replace
            # the route with the serial and make that telemetry impossible at
            # Core runtime.
            self._trusted_route_ids[id(candidate)] = device_key
        if serial:
            self._by_serial[serial] = candidate
        if device_key:
            self._by_device_key[device_key] = candidate
            self._sensitive.add(device_key)
        if product_key:
            self._sensitive.add(product_key)
        if product_key and device_key:
            self._by_pk_dk[(product_key, device_key)] = candidate

    def observe(self, topic, payload=None):
        if self._topic_count >= MAX_CLOUD_TOPICS:
            return
        self._topic_count += 1
        match = classify_topic(topic)
        if match.family == FAMILY_UNKNOWN:
            return
        candidate = self._match_candidate(match)
        if candidate is None:
            candidate = self._new_topic_only(match)
            if candidate is None:
                return
        route_id = match.device_id or match.serial_number
        if route_id:
            self._trusted_route_ids[id(candidate)] = route_id
        candidate.discovery_status = DISCOVERY_MQTT_OBSERVED
        candidate.topic_family = match.family
        redacted = _redact_topic(topic, self._sensitive)
        if redacted not in candidate.topics_seen and (
            len(candidate.topics_seen) < MAX_TOPICS_PER_CANDIDATE
        ):
            candidate.topics_seen.append(redacted)
        if match.metric:
            self._add_metric(candidate, match.metric)
        if payload is not None and match.family in _JSON_FAMILIES:
            parsed = parse_report_payload(payload)
            if parsed.get("serial_number") and not candidate.serial_number:
                candidate.serial_number = parsed["serial_number"]
            if parsed.get("model_hint") and not candidate.model_hint:
                candidate.model_hint = parsed["model_hint"]
            for metric in parsed.get("metrics", []):
                self._add_metric(candidate, metric)
            if parsed.get("pack_data"):
                self._pack_seen.add(id(candidate))
        candidate.confidence = _score(candidate, id(candidate) in self._pack_seen)

    def _match_candidate(self, match):
        if match.product_key and match.device_id:
            found = self._by_pk_dk.get((match.product_key, match.device_id))
            if found is not None:
                return found
        token = match.serial_number or match.device_id
        if token:
            return self._by_serial.get(token) or self._by_device_key.get(token)
        return None

    def _new_topic_only(self, match):
        if len(self._candidates) >= MAX_CLOUD_CANDIDATES:
            return None
        token = match.serial_number or match.device_id
        candidate = MqttHardwareCandidate(
            broker_id=self.broker_id,
            broker_host=self.broker_host,
            broker_port=self.broker_port,
            topic_family=match.family,
            device_id=token if match.serial_number else mask_id(token),
            serial_number=match.serial_number,
            display_name="MQTT topic only",
            source_type=SOURCE_ZENDURE_CLOUD_MQTT,
            source_label=SOURCE_LABEL,
            broker_label=f"{self.broker_host}:{self.broker_port}",
            discovery_status=DISCOVERY_MQTT_OBSERVED,
            tls_mode=self.tls_mode,
            auth_mode=AUTH_MODE_TOKEN,
        )
        self._candidates.append(candidate)
        if match.product_key:
            self._trusted_product_keys[id(candidate)] = match.product_key
        if match.serial_number:
            self._by_serial[match.serial_number] = candidate
        elif token:
            self._by_device_key[token] = candidate
        return candidate

    def _add_metric(self, candidate, metric):
        if metric not in candidate.metrics_seen and (
            len(candidate.metrics_seen) < MAX_METRICS_PER_CANDIDATE
        ):
            candidate.metrics_seen.append(metric)

    def candidates(self):
        return list(self._candidates)

    def results(self):
        return [candidate.to_dict() for candidate in self._candidates]

    def trusted_results(self):
        """Internal candidates with full write-target identity.

        This method is intentionally not used by discovery/status responses.
        The Admin server consumes it only to rebuild a selected proposal behind
        the authenticated config-preview/apply trust boundary.
        """

        results = []
        for candidate in self._candidates:
            data = candidate.to_dict()
            product_key = self._trusted_product_keys.get(id(candidate))
            if product_key:
                data["product_key"] = product_key
            route_id = self._trusted_route_ids.get(id(candidate))
            if route_id:
                data["device_id"] = route_id
            results.append(data)
        return results

    def mqtt_observed_count(self):
        return sum(
            1
            for candidate in self._candidates
            if candidate.discovery_status == DISCOVERY_MQTT_OBSERVED
        )


def _default_device_list_fetcher(credential, timeout):
    api_url, app_key = resolve_device_list_credential(credential)
    return fetch_device_list(api_url, app_key, timeout)


def _build_subscriptions(devices, app_key):
    subscriptions = []
    seen = set()

    def _add(topic):
        if topic not in seen and len(subscriptions) < MAX_CLOUD_SUBSCRIPTIONS:
            seen.add(topic)
            subscriptions.append(topic)

    for entry in devices[:MAX_CLOUD_DEVICES]:
        product_key = entry.get("productKey")
        device_key = entry.get("deviceKey")
        if product_key and device_key:
            _add(f"/{product_key}/{device_key}/#")
            _add(f"iot/{product_key}/{device_key}/#")
    if app_key:
        _add(f"{app_key}/#")
    return tuple(subscriptions)


def _effective_tls_port(mqtt):
    """Return the TLS port to connect on for a parsed deviceList ``mqtt`` block.

    Honour a non-plaintext port the API actually supplied; otherwise use the
    MQTT-over-TLS default (8883). The Zendure cloud broker serves TLS on 8883
    (self-signed cert) and plaintext on 1883, so a TLS handshake against 1883
    always fails. Discovery is TLS-only with no silent downgrade to plaintext,
    so a supplied 1883 (or no port) resolves to the TLS listener (8883) rather
    than attempting TLS against the plaintext port.
    """

    if mqtt.get("port_from_api") and mqtt.get("port"):
        port = int(mqtt["port"])
        if port != PLAINTEXT_MQTT_PORT:
            return port
    return DEFAULT_TLS_MQTT_PORT


class ZendureCloudDiscovery:
    """Cloud discovery service: token status, deviceList test, MQTT refresh."""

    def __init__(
        self,
        store=None,
        *,
        device_list_fetcher=None,
        listener_factory=None,
        timeout_s=DEFAULT_CLOUD_TIMEOUT_S,
        tls_mode=TLS_ENCRYPTED_NO_VERIFY,
    ):
        self.store = store or ZendureTokenStore()
        self._fetch = device_list_fetcher or _default_device_list_fetcher
        self._listener_factory = listener_factory or default_cloud_listener_factory
        self._timeout_s = max(0.0, min(float(timeout_s), MAX_CLOUD_TIMEOUT_S))
        self._tls_mode = tls_mode if tls_mode in TLS_MODES else TLS_SYSTEM_CA
        self._candidates = []
        self._trusted_candidates = []
        self._last_seed_attempt = 0.0

    # --- token management ------------------------------------------------

    def settings(self):
        return self.store.settings()

    def candidates(self):
        return list(self._candidates)

    def trusted_candidates(self):
        """Process-internal candidates used to resolve config selections."""

        return list(self._trusted_candidates)

    def ensure_device_list_candidates(self):
        """Best-effort deviceList-only seeding when nothing is cached yet.

        Candidates live only in memory and are cleared on restart, so a discovery
        run right after startup would otherwise have no Zendure devices and could
        not apply the configured priority to them. This lazily repopulates them
        from the saved API key. It never connects MQTT and never raises; a short
        cooldown avoids a slow deviceList call on every run when nothing is found.
        """

        if self._candidates:
            return len(self._candidates)
        now = time.monotonic()
        if now - self._last_seed_attempt < SEED_COOLDOWN_S:
            return 0
        self._last_seed_attempt = now
        api_key = self.store.load_token()
        if not api_key:
            return 0
        try:
            result = self._fetch(api_key, DEVICE_LIST_TIMEOUT_S)
        except Exception:
            return 0
        devices = result.get("devices", [])
        mqtt = result.get("mqtt", {})
        broker, tls_mode = self._broker_from_result(result)
        candidate_set = CloudCandidateSet(
            mqtt.get("host"),
            _effective_tls_port(mqtt),
            tls_mode,
            app_key=result.get("app_key"),
        )
        candidate_set.seed_devices(devices)
        self._candidates = candidate_set.results()
        self._trusted_candidates = candidate_set.trusted_results()
        try:
            self.store.update_metadata(
                last_checked=utc_now_iso(),
                last_status="ok",
                last_error=None,
                last_device_count=len(devices),
                last_broker=broker,
                tls_mode=tls_mode,
            )
        except Exception:
            pass
        return len(self._candidates)

    def save_token(self, api_key, *, validate=False):
        # Keep the original, trimmed credential: an HA token carries the region
        # URL needed by later test/refresh/runtime-provisioning calls. Resolve it
        # once before saving so a token with an untrusted embedded URL fails
        # closed without touching the credential store.
        credential = normalize_app_key(api_key)
        resolve_device_list_credential(credential)
        self.store.save_token(credential)
        if validate:
            self.test(api_key=credential)
        return {
            "ok": True,
            "token_saved": True,
            "message": "Zendure credential saved.",
        }

    def provision_runtime_credentials(self, credential_store, ref=None, transaction=None):
        """Persist the Core-resolvable cloud MQTT runtime credential record.

        Fetches a fresh deviceList (the broker credentials may rotate
        server-side), writes the record atomically through the credential store
        and verifies that the reference resolves through the Core resolver.
        Returns the newly created runtime refs (empty when an existing record
        was deliberately replaced) so a failed config write can roll back.
        Passing a ``transaction`` list records the pre-change snapshot so the
        caller can also restore a *rotated* record after a later apply failure.
        Raises ``CredentialStoreError`` with an actionable message when the
        account key is missing, the deviceList is unavailable, the response
        lacks any of the four required runtime fields (username, password,
        client_id, app_key), or the persisted record does not resolve back to
        that complete contract — and in the persisted case the record is
        already rolled back here (the previous value restored, or the new
        record removed), so the caller never needs a successful return value
        to know what to clean up.
        """

        from admin.credential_store import (
            ZENDURE_CLOUD_CREDENTIALS_REF,
            CredentialProvisioningError,
            CredentialStoreError,
            validate_resolved_mqtt_credential,
        )
        from ems.mqtt_credentials import FileMqttCredentialResolver, MqttCredentialError
        from ems.zendure_mqtt.config_entries import SOURCE_ZENDURE_CLOUD_MQTT

        ref = ref or ZENDURE_CLOUD_CREDENTIALS_REF
        ref = credential_store.normalize_ref(ref)
        api_key = self.store.load_token()
        if not api_key:
            raise CredentialStoreError(
                "Zendure account is not connected. Save the Zendure API key "
                "or HA/deviceList token before applying a cloud MQTT device."
            )
        try:
            result = self._fetch(api_key, DEVICE_LIST_TIMEOUT_S)
        except ZendureCloudError as exc:
            raise CredentialStoreError(
                "Zendure cloud MQTT credentials could not be fetched, so the "
                f"configuration was not applied: {exc}"
            ) from exc
        mqtt = result.get("mqtt", {}) if isinstance(result, dict) else {}
        fetched = {
            "username": mqtt.get("username"),
            "password": mqtt.get("password"),
            "client_id": mqtt.get("client_id"),
            "app_key": result.get("app_key") if isinstance(result, dict) else None,
        }
        verdict = validate_resolved_mqtt_credential(
            credentials_ref=ref, source=SOURCE_ZENDURE_CLOUD_MQTT, resolved=fetched
        )
        if verdict.status != "valid":
            raise CredentialStoreError(
                "The Zendure deviceList response did not carry a complete "
                f"runtime credential for '{ref}' ({verdict.reason}); the "
                "cloud broker cannot be provisioned."
            )
        change = credential_store.snapshot_mqtt_credential_change(ref)
        ref = credential_store.save_mqtt_cloud_runtime_secret(
            ref,
            username=fetched["username"],
            password=fetched["password"],
            client_id=fetched["client_id"],
            app_key=fetched["app_key"],
        )
        verification_error = None
        try:
            resolved = FileMqttCredentialResolver(
                credential_store.secrets_dir
            ).resolve(ref)
        except MqttCredentialError as exc:
            verification_error = str(exc)
        else:
            verdict = validate_resolved_mqtt_credential(
                credentials_ref=ref,
                source=SOURCE_ZENDURE_CLOUD_MQTT,
                resolved=resolved,
            )
            if verdict.status != "valid":
                verification_error = verdict.reason
        if verification_error is not None:
            # Verification failed after the save: undo the save here so the
            # previous record (if any) stays the active one. A failed undo must
            # ride on the raised error — the caller gets no staging result to
            # roll back, so this is the only channel that can report it.
            failed = credential_store.rollback_credential_changes([change])
            message = (
                f"The persisted cloud MQTT credential '{ref}' is not "
                f"runtime-usable: {verification_error}"
            )
            if failed:
                raise CredentialProvisioningError(
                    message
                    + " Rolling the record back also failed; inspect "
                    "config/secrets and restore or remove it manually.",
                    credentials_ref=ref,
                    rollback_failed_refs=failed,
                )
            raise CredentialStoreError(message)
        if transaction is not None:
            transaction.append(change)
        return [] if change.existed_before else [ref]

    def delete_token(self):
        result = self.store.delete_token()
        self._candidates = []
        self._trusted_candidates = []
        return {
            "ok": True,
            "token_saved": False,
            "message": "Zendure credential removed.",
            "removed": bool(result.get("removed")),
        }

    def test(self, *, api_key=None):
        api_key = api_key or self.store.load_token()
        if not api_key:
            return {"ok": False, "error": "not_configured", "message": _ERR_NOT_CONFIGURED}
        try:
            result = self._fetch(api_key, DEVICE_LIST_TIMEOUT_S)
        except ZendureCloudError as exc:
            self.store.update_metadata(
                last_checked=utc_now_iso(),
                last_status="error",
                last_error=str(exc),
            )
            return {"ok": False, "error": "device_list_failed", "message": str(exc)}
        broker, tls_mode = self._broker_from_result(result)
        self.store.update_metadata(
            last_checked=utc_now_iso(),
            last_status="ok",
            last_error=None,
            last_device_count=len(result.get("devices", [])),
            last_broker=broker,
            tls_mode=tls_mode,
        )
        return {
            "ok": True,
            "devices_found": len(result.get("devices", [])),
            "broker": broker,
            "tls_required": True,
            "tls_mode": tls_mode,
            "message": "Zendure device list loaded.",
        }

    def refresh(self):
        api_key = self.store.load_token()
        if not api_key:
            return {"ok": False, "error": "not_configured", "message": _ERR_NOT_CONFIGURED}
        try:
            result = self._fetch(api_key, DEVICE_LIST_TIMEOUT_S)
        except ZendureCloudError as exc:
            self.store.update_metadata(
                last_checked=utc_now_iso(),
                last_status="error",
                last_error=str(exc),
            )
            return {"ok": False, "error": "device_list_failed", "message": str(exc)}

        devices = result.get("devices", [])
        mqtt = result.get("mqtt", {})
        app_key = result.get("app_key")
        broker, tls_mode = self._broker_from_result(result)
        effective_port = _effective_tls_port(mqtt)
        candidate_set = CloudCandidateSet(
            mqtt.get("host"), effective_port, tls_mode, app_key=app_key
        )
        candidate_set.seed_devices(devices)

        connection = CloudMqttConnection(
            host=mqtt.get("host"),
            port=effective_port,
            username=mqtt.get("username"),
            password=mqtt.get("password"),
            client_id=mqtt.get("client_id"),
            subscriptions=_build_subscriptions(devices, app_key),
            tls_enabled=True,
            tls_mode=tls_mode,
        )
        mqtt_error = None
        try:
            listener = self._listener_factory(connection)
            listener.listen(self._timeout_s, candidate_set.observe)
        except CloudMqttError as exc:
            mqtt_error = str(exc)
        except Exception:  # a listener fault must never crash the Admin route
            mqtt_error = _ERR_MQTT_FAILED

        self._candidates = candidate_set.results()
        self._trusted_candidates = candidate_set.trusted_results()
        observed = candidate_set.mqtt_observed_count()
        # Reaching this point means the deviceList succeeded, so the discovery run
        # is "ok" even if the best-effort live-MQTT enrichment failed. Only a
        # deviceList failure (handled above) is a hard error; a failed/empty MQTT
        # listen is surfaced as the transient ``mqtt_status``/``mqtt_message`` in
        # the payload, not as a persisted "error" that hides the found devices.
        self.store.update_metadata(
            last_checked=utc_now_iso(),
            last_status="ok",
            last_error=None,
            last_device_count=len(devices),
            last_broker=broker,
            tls_mode=tls_mode,
        )
        payload = {
            "ok": True,
            "device_list_count": len(devices),
            "mqtt_observed_count": observed,
            "devices_found": len(self._candidates),
            "tls_mode": tls_mode,
            "broker": broker,
            "candidates": self._candidates,
        }
        if mqtt_error:
            payload["mqtt_status"] = "error"
            payload["mqtt_message"] = mqtt_error
        elif observed == 0:
            payload["mqtt_status"] = "no_telemetry"
            payload["mqtt_message"] = _ERR_NO_TELEMETRY
        else:
            payload["mqtt_status"] = "ok"
        return payload

    def _broker_from_result(self, result):
        mqtt = result.get("mqtt", {}) if isinstance(result, dict) else {}
        host = mqtt.get("host") or "unknown"
        port = _effective_tls_port(mqtt)
        return f"{host}:{port}", self._tls_mode
