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
from collections import OrderedDict, deque
from dataclasses import dataclass

from ems.zendure_mqtt.client import ZendureMqttReadClient
from ems.zendure_mqtt.config import ZendureMqttClientConfig
from ems.zendure_mqtt.service import ZendureMqttService

logger = logging.getLogger(__name__)

# How many recently-delivered publish mids are remembered for delivery checks.
_DELIVERED_MID_HISTORY = 512
_DELIVERY_TOKEN_HISTORY = 512
_MAX_MQTT_PACKET_IDENTIFIER = 65_535

DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_DISCONNECTED = "disconnected"
DELIVERY_EXPIRED = "expired"
DELIVERY_UNKNOWN = "unknown"


@dataclass(frozen=True)
class PublishDeliveryToken:
    """Process-local identity for exactly one accepted MQTT publish."""

    connection_generation: int
    sequence: int
    mid: int


@dataclass(frozen=True)
class PublishSubmission:
    """Local outcome of handing one message to the MQTT client.

    ``accepted`` means the client queued the message for transmission — nothing
    more. ``delivery_token`` identifies this exact submission even when paho
    later reuses ``mid``. Both are ``None`` when no transport evidence exists.
    """

    accepted: bool
    mid: int | None = None
    delivery_token: PublishDeliveryToken | None = None

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
        self._delivery_states: OrderedDict[PublishDeliveryToken, str] = OrderedDict()
        self._pending_delivery_tokens: dict[int, deque] = {}
        # A callback carries only a raw 16-bit MID. Once unresolved evidence for
        # that MID loses its tombstone, no later callback can be attributed
        # safely. Quarantine it for this client's lifetime (bounded by MQTT's
        # protocol identifier space) rather than risk confirming a reused MID.
        self._quarantined_delivery_mids: set[int] = set()
        self._early_pubacks: dict[tuple[int, int], int] = {}
        self._connection_generation = 0
        self._publish_sequence = 0
        self._publish_lock = threading.Lock()
        self._publish_in_progress_generation: int | None = None

    def start(self):
        super().start()
        client = self._client
        if client is not None:
            client.on_publish = self._on_publish

    def _on_publish(self, _client, _userdata, mid, *_args, **_kwargs):
        # paho v1: (client, userdata, mid); v2 adds reason_code/properties.
        with self._delivery_lock:
            quarantined = mid in self._quarantined_delivery_mids
            if not quarantined:
                if len(self._delivered_order) == self._delivered_order.maxlen:
                    oldest = self._delivered_order[0]
                    self._delivered_mids.discard(oldest)
                self._delivered_order.append(mid)
                self._delivered_mids.add(mid)
            delivery_key = (self._connection_generation, mid)
            pending = (
                None if quarantined else self._pending_delivery_tokens.get(mid)
            )
            token = None
            while pending:
                candidate = pending.popleft()
                if candidate in self._delivery_states:
                    token = candidate
                    break
            if pending is not None and not pending:
                self._pending_delivery_tokens.pop(mid, None)
            if (
                token is not None
                and self._delivery_states.get(token) == DELIVERY_PENDING
            ):
                self._delivery_states[token] = DELIVERY_DELIVERED
            elif (
                token is None
                and not quarantined
                and self._publish_in_progress_generation
                == self._connection_generation
            ):
                self._early_pubacks[delivery_key] = (
                    self._early_pubacks.get(delivery_key, 0) + 1
                )
        if quarantined:
            logger.debug(
                "event=mqtt_publish_delivery_ignored mid=%s reason=quarantined",
                mid,
            )
        else:
            logger.debug("event=mqtt_publish_delivered mid=%s", mid)

    def delivery_confirmed(self, mid) -> bool:
        """Whether the broker acknowledged the publish with this mid."""

        if mid is None:
            return False
        with self._delivery_lock:
            if isinstance(mid, PublishDeliveryToken):
                return self._delivery_states.get(mid) == DELIVERY_DELIVERED
            return mid in self._delivered_mids

    def delivery_status(self, evidence) -> str:
        """Return the terminal/pending state for one submission token."""

        if evidence is None:
            return DELIVERY_UNKNOWN
        with self._delivery_lock:
            if isinstance(evidence, PublishDeliveryToken):
                return self._delivery_states.get(evidence, DELIVERY_EXPIRED)
            return (
                DELIVERY_DELIVERED
                if evidence in self._delivered_mids
                else DELIVERY_UNKNOWN
            )

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
        with self._delivery_lock:
            disconnected_mids = set()
            for token, state in self._delivery_states.items():
                if state == DELIVERY_PENDING:
                    self._delivery_states[token] = DELIVERY_DISCONNECTED
                    disconnected_mids.add(token.mid)
            for mid in disconnected_mids:
                self._quarantine_delivery_mid_locked(mid)
            self._connection_generation += 1
            self._early_pubacks.clear()
        super()._on_connect(client, *args, **kwargs)
        for topic in self._reply_topics:
            try:
                client.subscribe(topic, qos=0)
            except Exception:
                logger.debug("event=zendure_mqtt_reply_subscribe_failed")

    def _on_disconnect(self, *args, **kwargs):
        with self._delivery_lock:
            generation = self._connection_generation
            disconnected_mids = set()
            for token, state in self._delivery_states.items():
                if (
                    token.connection_generation == generation
                    and state == DELIVERY_PENDING
                ):
                    self._delivery_states[token] = DELIVERY_DISCONNECTED
                    disconnected_mids.add(token.mid)
            for mid in disconnected_mids:
                self._quarantine_delivery_mid_locked(mid)
            self._early_pubacks.clear()
        super()._on_disconnect(*args, **kwargs)

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
        with self._publish_lock:
            with self._delivery_lock:
                generation = self._connection_generation
                self._publish_in_progress_generation = generation
            try:
                info = client.publish(topic, payload, qos=qos, retain=retain)
            except Exception:  # a broker/publish error must never crash control
                logger.debug("event=zendure_mqtt_publish_failed")
                with self._delivery_lock:
                    self._publish_in_progress_generation = None
                return PublishSubmission(False)
            mid = getattr(info, "mid", None)
            if int(getattr(info, "rc", 0)) != 0:
                with self._delivery_lock:
                    self._publish_in_progress_generation = None
                return PublishSubmission(False)
            if mid is None:
                with self._delivery_lock:
                    self._publish_in_progress_generation = None
                return PublishSubmission(True)
            with self._delivery_lock:
                self._publish_sequence += 1
                token = PublishDeliveryToken(generation, self._publish_sequence, mid)
                delivery_key = (generation, mid)
                early_count = self._early_pubacks.pop(delivery_key, 0)
                self._early_pubacks.clear()
                disconnected_before_registration = (
                    not self._connected
                    or generation != self._connection_generation
                )
                if mid in self._quarantined_delivery_mids:
                    state = DELIVERY_EXPIRED
                elif early_count:
                    state = DELIVERY_DELIVERED
                elif disconnected_before_registration:
                    state = DELIVERY_DISCONNECTED
                else:
                    state = DELIVERY_PENDING
                    self._pending_delivery_tokens.setdefault(
                        mid, deque()
                    ).append(token)
                self._delivery_states[token] = state
                if state == DELIVERY_DISCONNECTED:
                    self._quarantine_delivery_mid_locked(mid)
                self._publish_in_progress_generation = None
                self._prune_delivery_states_locked()
            return PublishSubmission(True, mid, token)

    def _prune_delivery_states_locked(self):
        while len(self._delivery_states) > _DELIVERY_TOKEN_HISTORY:
            token, state = self._delivery_states.popitem(last=False)
            if state in (DELIVERY_PENDING, DELIVERY_DISCONNECTED):
                self._quarantine_delivery_mid_locked(token.mid)
                continue
            pending = self._pending_delivery_tokens.get(token.mid)
            if pending is None:
                continue
            self._pending_delivery_tokens[token.mid] = deque(
                item for item in pending if item != token
            )
            if not self._pending_delivery_tokens[token.mid]:
                self._pending_delivery_tokens.pop(token.mid, None)

    def _quarantine_delivery_mid_locked(self, mid):
        """Make an ambiguous protocol MID permanently non-attributable."""

        if (
            isinstance(mid, bool)
            or not isinstance(mid, int)
            or not 1 <= mid <= _MAX_MQTT_PACKET_IDENTIFIER
        ):
            return
        self._quarantined_delivery_mids.add(mid)
        pending = self._pending_delivery_tokens.pop(mid, ())
        for token in pending:
            if self._delivery_states.get(token) == DELIVERY_PENDING:
                self._delivery_states[token] = DELIVERY_EXPIRED
        for key in tuple(self._early_pubacks):
            if key[1] == mid:
                self._early_pubacks.pop(key, None)


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

    def delivery_status(self, evidence) -> str:
        """Return delivery evidence for one accepted publish submission."""

        client = self._client
        if client is None:
            return DELIVERY_DISCONNECTED
        status = getattr(client, "delivery_status", None)
        if callable(status):
            return status(evidence)
        return (
            DELIVERY_DELIVERED
            if self.delivery_confirmed(evidence)
            else DELIVERY_UNKNOWN
        )
