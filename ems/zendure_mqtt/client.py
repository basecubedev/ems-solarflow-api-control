# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only MQTT client feeding Zendure telemetry into the aggregator.

Subscribes to the known Zendure topic families and forwards every message to a
``ZendureMqttAggregator``. It is read-only by construction: it exposes no
publish/write/control path, and it never logs or returns credentials. Malformed
payloads and unknown topics are absorbed by the aggregator without crashing the
network loop.
"""

import logging
import threading

# Re-exported for compatibility: the error class lives in the paho-free config
# module so its identity is stable for consumers that ship no client module.
from ems.zendure_mqtt.config import ZendureMqttClientConfig, ZendureMqttClientError
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator

logger = logging.getLogger(__name__)


def _default_client_factory(config: ZendureMqttClientConfig):
    import paho.mqtt.client as mqtt

    client_id = config.client_id or ""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):  # paho < 2.0 has no versioned ctor
        return mqtt.Client(client_id=client_id)


class ZendureMqttReadClient:
    """Connects to a broker and mirrors Zendure telemetry into snapshots.

    ``start`` connects and subscribes; ``stop`` disconnects and is safe to call
    repeatedly. Reconnects are handled by the underlying paho loop, and
    subscriptions are re-issued on every (re)connect via ``on_connect``.
    """

    def __init__(
        self,
        config: ZendureMqttClientConfig,
        aggregator: ZendureMqttAggregator | None = None,
        *,
        client_factory=None,
    ):
        self._config = config
        self._aggregator = aggregator if aggregator is not None else ZendureMqttAggregator()
        self._client_factory = client_factory or _default_client_factory
        self._client = None
        self._lock = threading.Lock()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._client is not None:
            return
        client = self._client_factory(self._config)
        # Local import: ems.config imports no zendure_mqtt module, but sibling
        # modules import ems.config lazily by convention to avoid cycles.
        from ems.config import configure_mqtt_client_tls

        configure_mqtt_client_tls(
            client,
            tls=self._config.tls,
            tls_insecure=self._config.tls_insecure,
        )
        if self._config.username is not None:
            client.username_pw_set(self._config.username, self._config.password)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        connect_timeout = max(1.0, float(self._config.connect_timeout_seconds))
        if hasattr(client, "connect_timeout"):
            client.connect_timeout = connect_timeout
        try:
            client.connect(
                self._config.host,
                self._config.port,
                keepalive=max(2, int(self._config.keepalive_seconds)),
            )
            client.loop_start()
        except Exception as exc:  # never surface broker/socket/secret detail
            self._safe_stop(client)
            raise ZendureMqttClientError(
                f"failed to connect to MQTT broker at "
                f"{self._config.host}:{self._config.port}: {type(exc).__name__}"
            ) from None
        self._client = client

    def stop(self) -> None:
        client = self._client
        self._client = None
        self._connected = False
        if client is not None:
            self._safe_stop(client)

    def snapshots(self):
        """Latest per-device snapshots keyed by device id."""

        with self._lock:
            return {snap.device_id: snap for snap in self._aggregator.snapshots()}

    @staticmethod
    def _safe_stop(client) -> None:
        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, *_args, **_kwargs):
        self._connected = True
        for topic in self._config.resolved_subscriptions():
            try:
                client.subscribe(topic, qos=0)
            except Exception:
                logger.debug("event=zendure_mqtt_subscribe_failed topic=%s", topic)

    def _on_disconnect(self, *_args, **_kwargs):
        self._connected = False

    def _on_message(self, _client, _userdata, message):
        try:
            self._observe(message.topic, message.payload)
        except Exception:  # a hostile broker must never crash the network loop
            logger.debug("event=zendure_mqtt_message_dropped")

    def _observe(self, topic, payload) -> None:
        with self._lock:
            self._aggregator.observe(topic, payload)
