# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the mapping connects, and one MQTT connection per broker.

The Admin renders ``<install>/mqtt/mapping-credentials.json`` with exactly the
brokers the mappings use and the account for each; the service reads nothing
else to connect, so it needs neither the EMS configuration nor the Admin's
secret store. A connection speaks MQTT 5 so a retained state can expire, and
falls back to 3.1.1 for a broker that refuses version 5, which it reports.
"""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass

from ems.config import configure_mqtt_client_tls

CREDENTIALS_SCHEMA_VERSION = 1
KEEPALIVE_SECONDS = 30
RECONNECT_MIN_SECONDS = 1
RECONNECT_MAX_SECONDS = 30
_PROTOCOL_REFUSED = frozenset({1, 132})


@dataclass(frozen=True)
class BrokerEndpoint:
    ref: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    tls: bool = False
    tls_insecure: bool = False


def _endpoint(ref, raw):
    if not isinstance(raw, dict):
        return None
    host, port = raw.get("host"), raw.get("port")
    if not isinstance(host, str) or not host.strip():
        return None
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    username, password = raw.get("username"), raw.get("password")
    if username is not None and (not isinstance(username, str) or not isinstance(password, str)):
        return None
    tls, tls_insecure = raw.get("tls", False), raw.get("tls_insecure", False)
    if not isinstance(tls, bool) or not isinstance(tls_insecure, bool) or (tls_insecure and not tls):
        return None
    return BrokerEndpoint(ref, host.strip(), port, username, password, tls, tls_insecure)


def _unreadable(path, message):
    return {"code": "mqtt_mapping_brokers_unreadable", "path": path, "message": message}


def load_broker_file(path):
    """``({ref: BrokerEndpoint}, issues)``; a missing file means no brokers yet."""

    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return {}, []
    except (OSError, ValueError):
        return {}, [_unreadable("", "The broker file cannot be read.")]
    if not isinstance(document, dict) or document.get("schema_version") != CREDENTIALS_SCHEMA_VERSION:
        return {}, [_unreadable("schema_version", "The broker file has an unknown form.")]
    brokers = document.get("brokers")
    if not isinstance(brokers, dict):
        return {}, [_unreadable("brokers", "The broker file lists no brokers.")]
    endpoints, issues = {}, []
    for ref, raw in brokers.items():
        endpoint = _endpoint(ref, raw)
        if endpoint is None:
            issues.append(_unreadable(f"brokers.{ref}", "This broker entry is incomplete."))
        else:
            endpoints[ref] = endpoint
    return endpoints, issues


def _reason_value(reason_code):
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class PahoConnection:
    """One broker: subscribe to the source topics, publish retained states with expiry.

    ``on_message(topic, payload)`` is called from paho's network thread.
    """

    def __init__(self, endpoint: BrokerEndpoint, topics, on_message: Callable[[str, bytes], None]):
        self.endpoint = endpoint
        self.topics = tuple(topics)
        self._on_message = on_message
        self._lock = threading.Lock()
        self._client = None
        self.connected = False
        self.mqtt5 = None
        self.error = None

    def _new_client(self, mqtt5):
        import paho.mqtt.client as mqtt

        protocol = mqtt.MQTTv5 if mqtt5 else mqtt.MQTTv311
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=protocol)
        configure_mqtt_client_tls(client, tls=self.endpoint.tls, tls_insecure=self.endpoint.tls_insecure)
        if self.endpoint.username is not None:
            client.username_pw_set(self.endpoint.username, self.endpoint.password)
        client.reconnect_delay_set(RECONNECT_MIN_SECONDS, RECONNECT_MAX_SECONDS)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        return client

    def start(self, *, mqtt5=True):
        with self._lock:
            self.mqtt5 = mqtt5
            self._client = self._new_client(mqtt5)
            client = self._client
        try:
            client.connect_async(self.endpoint.host, self.endpoint.port, keepalive=KEEPALIVE_SECONDS)
        except (OSError, ValueError) as exc:
            self.error = f"connect_failed:{type(exc).__name__}"
        client.loop_start()

    def stop(self):
        with self._lock:
            client, self._client = self._client, None
            self.connected = False
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            pass
        client.loop_stop()

    def _handle_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        code = _reason_value(reason_code)
        if code not in (0, None) and getattr(reason_code, "is_failure", True):
            self.connected = False
            if self.mqtt5 and code in _PROTOCOL_REFUSED:
                self.error = "mqtt5_unsupported"
                threading.Thread(target=self._fall_back, daemon=True).start()
                return
            self.error = "connect_refused"
            return
        self.connected = True
        self.error = None
        for topic in self.topics:
            client.subscribe(topic, qos=1)

    def _fall_back(self):
        self.stop()
        self.start(mqtt5=False)

    def _handle_disconnect(self, *_args, **_kwargs):
        self.connected = False

    def _handle_message(self, _client, _userdata, message):
        self._on_message(message.topic, message.payload)

    def publish(self, topic, payload, expiry_seconds) -> bool:
        with self._lock:
            client = self._client
        if client is None or not self.connected:
            return False
        properties = None
        if self.mqtt5:
            from paho.mqtt.packettypes import PacketTypes
            from paho.mqtt.properties import Properties

            properties = Properties(PacketTypes.PUBLISH)
            properties.MessageExpiryInterval = int(expiry_seconds)
        info = client.publish(topic, payload, qos=1, retain=True, properties=properties)
        return info.rc == 0

    def state(self) -> dict:
        return {"connected": self.connected, "mqtt5": self.mqtt5, "error": self.error}
