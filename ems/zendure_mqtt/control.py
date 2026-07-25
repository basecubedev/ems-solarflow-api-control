# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zendure MQTT control client and per-broker control service.

The write-capable variant of the read-only telemetry stack: a single narrow
publish path for control/property writes. Whether a write is allowed is decided
upstream by the EMS write gates; this module only performs an already-approved
publish. It carries the built message's QoS/retain to the paho client unchanged
and tracks per-mid broker delivery (PUBACK/on_publish) so a locally-accepted
publish is never conflated with one the broker acknowledged.
"""

import logging
import threading
from collections import deque
from dataclasses import dataclass

from ems.zendure_mqtt.client import ZendureMqttReadClient
from ems.zendure_mqtt.config import ZendureMqttClientConfig
from ems.zendure_mqtt.service import ZendureMqttService

logger = logging.getLogger(__name__)

# How many recently-delivered publish mids are remembered for delivery checks.
_DELIVERED_MID_HISTORY = 512


@dataclass(frozen=True)
class PublishSubmission:
    """Local outcome of handing one message to the MQTT client.

    ``accepted`` means the client queued the message for transmission — nothing
    more. ``mid`` correlates a later broker delivery acknowledgement; it is
    ``None`` when the client rejected the message or exposes no mid.
    """

    accepted: bool
    mid: int | None = None

    def __bool__(self) -> bool:
        return self.accepted


class ZendureMqttControlClient(ZendureMqttReadClient):
    """Read client that can also publish gated ``outputLimit`` writes.

    It additionally subscribes to registered device *reply* topics and dispatches
    those messages to reply handlers. Reply messages are correlated by payload
    (messageId + deviceId) inside each handler, so a reply is broadcast to every
    handler and a non-matching one is ignored — a broker publish is never treated
    as device acceptance.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._reply_topics: set[str] = set()
        self._reply_handlers: list = []
        self._delivery_lock = threading.Lock()
        self._delivered_mids: set[int] = set()
        self._delivered_order: deque = deque(maxlen=_DELIVERED_MID_HISTORY)

    def start(self):
        super().start()
        client = self._client
        if client is not None:
            client.on_publish = self._on_publish

    def _on_publish(self, _client, _userdata, mid, *_args, **_kwargs):
        # paho v1: (client, userdata, mid); v2 adds reason_code/properties.
        with self._delivery_lock:
            if len(self._delivered_order) == self._delivered_order.maxlen:
                oldest = self._delivered_order[0]
                self._delivered_mids.discard(oldest)
            self._delivered_order.append(mid)
            self._delivered_mids.add(mid)
        logger.debug("event=mqtt_publish_delivered mid=%s", mid)

    def delivery_confirmed(self, mid) -> bool:
        """Whether the broker acknowledged the publish with this mid."""

        if mid is None:
            return False
        with self._delivery_lock:
            return mid in self._delivered_mids

    def register_reply_handler(self, topics, handler):
        new_topics = [t for t in topics if isinstance(t, str) and t]
        for topic in new_topics:
            self._reply_topics.add(topic)
        if handler is not None and handler not in self._reply_handlers:
            self._reply_handlers.append(handler)
        client = self._client
        if client is not None and self._connected:
            for topic in new_topics:
                try:
                    client.subscribe(topic, qos=0)
                except Exception:
                    logger.debug("event=zendure_mqtt_reply_subscribe_failed")

    def _on_connect(self, client, *args, **kwargs):
        super()._on_connect(client, *args, **kwargs)
        for topic in self._reply_topics:
            try:
                client.subscribe(topic, qos=0)
            except Exception:
                logger.debug("event=zendure_mqtt_reply_subscribe_failed")

    def _on_message(self, client, userdata, message):
        topic = getattr(message, "topic", None)
        if topic in self._reply_topics:
            self._dispatch_reply(getattr(message, "payload", None))
            return
        super()._on_message(client, userdata, message)

    def _dispatch_reply(self, payload):
        for handler in list(self._reply_handlers):
            try:
                handler(payload)
            except Exception:  # a hostile reply must never crash the loop
                logger.debug("event=zendure_mqtt_reply_handler_failed")

    def publish(self, topic, payload, qos=0):
        """Publish one message; return success. Never raises."""

        return self.publish_message_meta(
            topic, payload, qos=qos, retain=False
        ).accepted

    def publish_message(self, message) -> PublishSubmission:
        """Publish a prepared :class:`MqttPublishMessage`, metadata intact."""

        return self.publish_message_meta(
            message.topic,
            message.payload,
            qos=message.qos,
            retain=message.retain,
        )

    def publish_message_meta(self, topic, payload, *, qos, retain) -> PublishSubmission:
        """Hand one message to the paho client; report local acceptance + mid.

        Local acceptance is transport-level only — broker delivery is observed
        separately via :meth:`delivery_confirmed`. Never raises.
        """

        client = self._client
        if client is None or not self._connected:
            return PublishSubmission(False)
        try:
            info = client.publish(topic, payload, qos=qos, retain=retain)
        except Exception:  # a broker/publish error must never crash control
            logger.debug("event=zendure_mqtt_publish_failed")
            return PublishSubmission(False)
        if int(getattr(info, "rc", 0)) != 0:
            return PublishSubmission(False)
        return PublishSubmission(True, getattr(info, "mid", None))


def _default_control_client_factory(
    config: ZendureMqttClientConfig,
) -> ZendureMqttControlClient:
    return ZendureMqttControlClient(config)


class ZendureMqttControlService(ZendureMqttService):
    """One broker's telemetry + control channel, reusing the read service."""

    def __init__(self, config, *, read_client_factory=None):
        super().__init__(
            config,
            read_client_factory=read_client_factory or _default_control_client_factory,
        )
        # Reply-handler registrations survive (re)connects: they are re-applied to
        # the client each time the service starts.
        self._reply_registrations: list = []

    def register_reply_handler(self, topics, handler):
        """Route reply-topic messages to ``handler``; also applies live."""

        self._reply_registrations.append((tuple(topics), handler))
        client = self._client
        if client is not None and hasattr(client, "register_reply_handler"):
            client.register_reply_handler(topics, handler)

    def start(self):
        super().start()
        client = self._client
        if client is not None and hasattr(client, "register_reply_handler"):
            for topics, handler in self._reply_registrations:
                client.register_reply_handler(topics, handler)

    def publish_output_limit(self, topic, payload):
        """Publish a prepared control message; return success (False when down)."""

        client = self._client
        if client is None:
            return False
        return client.publish(topic, payload)

    def publish_message(self, message) -> PublishSubmission:
        """Publish a prepared message with its QoS/retain metadata intact."""

        client = self._client
        if client is None:
            return PublishSubmission(False)
        return client.publish_message(message)

    def delivery_confirmed(self, mid) -> bool:
        """Whether the broker acknowledged the publish with this mid."""

        client = self._client
        if client is None:
            return False
        confirmed = getattr(client, "delivery_confirmed", None)
        return bool(callable(confirmed) and confirmed(mid))
