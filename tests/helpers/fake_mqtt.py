# SPDX-License-Identifier: AGPL-3.0-or-later
"""One reusable fake MQTT broker/client harness for the EMS test suite.

The production stack talks to two paho-compatible surfaces:

* the Zendure telemetry/control clients (``connect`` + ``loop_start``, subscribe
  in ``on_connect``, ``publish`` returns an object with ``rc``), and
* :class:`ems.clients.MqttGridMeterClient` (``connect_async`` + ``loop_start``,
  ``on_connect(client, userdata, flags, rc)``, never publishes).

Both are served by the same :class:`FakeMqttClient`. A :class:`FakeBroker` owns
the clients bound to one virtual broker and records every publish tagged with its
``ref`` so cross-broker leakage is trivial to assert. :class:`FakeMqttNetwork`
maps ``broker_ref`` to a broker and builds the injection points the production
builders expect (a control ``service_factory`` and a grid-meter
``_mqtt_client_factory``).

Failure modes (connect failure, publish failure, forced disconnect) are opt-in
per broker so degraded-broker scenarios stay deterministic. No real sockets, no
threads, no sleeps.
"""

from dataclasses import dataclass
from types import SimpleNamespace

__all__ = [
    "PublishRecord",
    "FakeMqttClient",
    "FakeBroker",
    "FakeMqttNetwork",
    "FakeClock",
    "topic_matches",
]


class FakeClock:
    """Deterministic monotonic clock for staleness tests (no real sleeps).

    Injected into the telemetry aggregators so a snapshot's ``last_seen`` uses it;
    :func:`patch_snapshot_clock` points the runtime's staleness math at the same
    value so ``advance`` moves a device from fresh to stale with no wall-clock wait.
    """

    def __init__(self, start: float = 10_000.0):
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


def topic_matches(topic_filter: str, topic: str) -> bool:
    """Standard MQTT wildcard match (``+`` single level, ``#`` multi level)."""

    if topic_filter == topic:
        return True
    filter_parts = topic_filter.split("/")
    topic_parts = topic.split("/")
    for index, part in enumerate(filter_parts):
        if part == "#":
            return True
        if index >= len(topic_parts):
            return False
        if part == "+":
            continue
        if part != topic_parts[index]:
            return False
    return len(filter_parts) == len(topic_parts)


@dataclass(frozen=True)
class PublishRecord:
    """One captured publish, tagged with the broker it was sent to."""

    broker_ref: str
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False

    @property
    def is_write(self) -> bool:
        return self.topic.endswith("properties/write")


class FakeMqttClient:
    """paho-compatible client bound to a :class:`FakeBroker`.

    Serves both the Zendure clients and the grid-meter client. Records captured
    TLS/auth so tests can prove no credential crosses brokers, and defers all
    message routing to its broker.
    """

    def __init__(self, broker: "FakeBroker"):
        self._broker = broker
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.subscriptions: list[str] = []
        self.tls_enabled = False
        self.tls_insecure = False
        self.username = None
        self.password = None
        self.connected = False
        self._loop_running = False
        self.connect_count = 0
        self.loop_start_count = 0
        self.loop_stop_count = 0
        self.disconnect_count = 0

    # --- connection setup ---------------------------------------------------
    def tls_set(self, *args, **kwargs):
        self.tls_enabled = True

    def tls_insecure_set(self, value):
        self.tls_insecure = bool(value)

    def username_pw_set(self, username, password=None):
        self.username = username
        self.password = password

    def connect(self, host, port, keepalive=0):
        self.connect_count += 1
        self._broker.record_endpoint(host, port)
        if self._broker.connect_fails:
            raise ConnectionError("fake broker refused the connection")

    def connect_async(self, host, port, keepalive=0):
        # The grid-meter client uses connect_async; failure must propagate so the
        # client records a sanitized connect error instead of subscribing.
        self.connect(host, port, keepalive=keepalive)

    def loop_start(self):
        self._loop_running = True
        self.loop_start_count += 1
        self._fire_connect()

    def loop_stop(self):
        self._loop_running = False
        self.loop_stop_count += 1

    def disconnect(self):
        self.connected = False
        self.disconnect_count += 1
        if self.on_disconnect is not None:
            self.on_disconnect(self, None, 0)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        rc = 1 if self._broker.publish_fails else 0
        self._broker.record_publish(
            PublishRecord(self._broker.ref, topic, payload, qos=qos, retain=retain)
        )
        return SimpleNamespace(rc=rc, mid=1)

    # --- routing ------------------------------------------------------------
    def _fire_connect(self):
        if self._broker.connect_fails:
            return
        self.connected = True
        if self.on_connect is not None:
            self.on_connect(self, None, None, 0)
        # Replay retained messages to a fresh subscriber, matching a real broker.
        for topic, payload in self._broker.retained.items():
            self.deliver(topic, payload)

    def deliver(self, topic, payload) -> bool:
        if not self.connected or self.on_message is None:
            return False
        if not any(topic_matches(sub, topic) for sub in self.subscriptions):
            return False
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload))
        return True

    def force_disconnect(self, rc=1):
        self.connected = False
        if self.on_disconnect is not None:
            self.on_disconnect(self, None, rc)


class FakeBroker:
    """One virtual MQTT broker. Records publishes and routes injected messages."""

    def __init__(
        self,
        ref: str = "default",
        *,
        connect_fails: bool = False,
        publish_fails: bool = False,
    ):
        self.ref = ref
        self.connect_fails = connect_fails
        self.publish_fails = publish_fails
        self.clients: list[FakeMqttClient] = []
        self.publish_calls: list[PublishRecord] = []
        self.retained: dict[str, object] = {}
        self.endpoints: list[tuple[str, int]] = []

    def client_factory(self) -> FakeMqttClient:
        client = FakeMqttClient(self)
        self.clients.append(client)
        return client

    def record_publish(self, record: PublishRecord) -> None:
        self.publish_calls.append(record)

    def record_endpoint(self, host, port) -> None:
        self.endpoints.append((host, port))

    # --- message injection --------------------------------------------------
    def inject(self, topic, payload, *, retain=False) -> bool:
        if retain:
            self.retained[topic] = payload
        delivered = False
        for client in list(self.clients):
            if client.deliver(topic, payload):
                delivered = True
        return delivered

    def disconnect_all(self, rc=1) -> None:
        for client in list(self.clients):
            client.force_disconnect(rc)

    # --- assertions helpers -------------------------------------------------
    @property
    def writes(self) -> list[PublishRecord]:
        return [record for record in self.publish_calls if record.is_write]

    @property
    def write_topics(self) -> list[str]:
        return [record.topic for record in self.writes]

    def captured_credentials(self) -> list[tuple]:
        return [
            (client.username, client.password)
            for client in self.clients
            if client.username is not None or client.password is not None
        ]


class FakeMqttNetwork:
    """Maps ``broker_ref`` to a :class:`FakeBroker` and wires the injection points.

    When a :class:`FakeClock` is supplied, every telemetry aggregator built by the
    service factories stamps snapshots with that clock, so staleness is driven by
    ``clock.advance`` instead of a real wait.
    """

    def __init__(self, clock: "FakeClock | None" = None):
        self.brokers: dict[str, FakeBroker] = {}
        self.clock = clock

    def broker(self, ref: str, **kwargs) -> FakeBroker:
        broker = self.brokers.get(ref)
        if broker is None:
            broker = FakeBroker(ref, **kwargs)
            self.brokers[ref] = broker
        return broker

    def _new_aggregator(self):
        if self.clock is None:
            return None
        from ems.zendure_mqtt.snapshot import ZendureMqttAggregator

        return ZendureMqttAggregator(monotonic=self.clock.monotonic)

    def control_service_factory(self):
        """A ``service_factory`` for ``build_zendure_mqtt_control_runtime``.

        Each broker profile gets a control service whose read client is backed by
        this network's fake broker for that ``broker_ref``.
        """

        from ems.zendure_mqtt.control import (
            ZendureMqttControlClient,
            ZendureMqttControlService,
        )

        def factory(broker_config):
            broker = self.broker(broker_config.broker_ref)
            return ZendureMqttControlService(
                broker_config,
                read_client_factory=lambda cfg: ZendureMqttControlClient(
                    cfg,
                    aggregator=self._new_aggregator(),
                    client_factory=lambda c: broker.client_factory(),
                ),
            )

        return factory

    def telemetry_service_factory(self):
        """A ``service_factory`` for ``build_zendure_mqtt_runtime``.

        Used for telemetry-only brokers that are not shared with a control
        service, so a read-only telemetry device never opens a real socket.
        """

        from ems.zendure_mqtt.client import ZendureMqttReadClient
        from ems.zendure_mqtt.service import ZendureMqttService

        def factory(broker_config):
            broker = self.broker(broker_config.broker_ref)
            return ZendureMqttService(
                broker_config,
                read_client_factory=lambda cfg: ZendureMqttReadClient(
                    cfg,
                    aggregator=self._new_aggregator(),
                    client_factory=lambda c: broker.client_factory(),
                ),
            )

        return factory

    def grid_meter_client_factory(self, ref: str):
        """A ``_mqtt_client_factory`` bound to one broker for the grid meter.

        Returns a plain function (not a bound method): the grid-meter settings
        dict is deep-copied by the config layer, and ``deepcopy`` treats a
        function as atomic but would otherwise traverse a bound method's live
        broker/client graph (which holds a thread lock).
        """

        broker = self.broker(ref)
        return lambda: broker.client_factory()

    def all_publishes(self) -> list[PublishRecord]:
        records: list[PublishRecord] = []
        for broker in self.brokers.values():
            records.extend(broker.publish_calls)
        return records
